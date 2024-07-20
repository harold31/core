"""The conversation platform for the Ollama integration."""

from __future__ import annotations

from collections.abc import Callable
import json
import logging
import time
from typing import Any, Literal

import ollama
import voluptuous as vol
from voluptuous_openapi import convert

from homeassistant.components import assist_pipeline, conversation
from homeassistant.components.conversation import trace
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_LLM_HASS_API, MATCH_ALL
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, TemplateError
from homeassistant.helpers import intent, llm, template
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import ulid

from .const import (
    CONF_MAX_HISTORY,
    CONF_MODEL,
    CONF_PROMPT,
    DEFAULT_MAX_HISTORY,
    DOMAIN,
    KEEP_ALIVE_FOREVER,
    MAX_HISTORY_SECONDS,
)
from .models import MessageHistory, MessageRole

# Max number of back and forth with the LLM to generate a response
MAX_TOOL_ITERATIONS = 10

_LOGGER = logging.getLogger(__name__)


def _format_tool(
    tool: llm.Tool, custom_serializer: Callable[[Any], Any] | None
) -> dict[str, Any]:
    """Format tool specification."""
    tool_spec = {
        "name": tool.name,
        "parameters": convert(tool.parameters, custom_serializer=custom_serializer),
    }
    if tool.description:
        tool_spec["description"] = tool.description
    return {"type": "function", "function": tool_spec}


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up conversation entities."""
    agent = OllamaConversationEntity(config_entry)
    async_add_entities([agent])


class OllamaConversationEntity(
    conversation.ConversationEntity, conversation.AbstractConversationAgent
):
    """Ollama conversation agent."""

    _attr_has_entity_name = True

    def __init__(self, entry: ConfigEntry) -> None:
        """Initialize the agent."""
        self.entry = entry

        # conversation id -> message history
        self._history: dict[str, MessageHistory] = {}
        self._attr_name = entry.title
        self._attr_unique_id = entry.entry_id

    async def async_added_to_hass(self) -> None:
        """When entity is added to Home Assistant."""
        await super().async_added_to_hass()
        assist_pipeline.async_migrate_engine(
            self.hass, "conversation", self.entry.entry_id, self.entity_id
        )
        conversation.async_set_agent(self.hass, self.entry, self)

    async def async_will_remove_from_hass(self) -> None:
        """When entity will be removed from Home Assistant."""
        conversation.async_unset_agent(self.hass, self.entry)
        await super().async_will_remove_from_hass()

    @property
    def supported_languages(self) -> list[str] | Literal["*"]:
        """Return a list of supported languages."""
        return MATCH_ALL

    async def async_process(
        self, user_input: conversation.ConversationInput
    ) -> conversation.ConversationResult:
        """Process a sentence."""
        settings = {**self.entry.data, **self.entry.options}
        options = self.entry.options

        client = self.hass.data[DOMAIN][self.entry.entry_id]
        conversation_id = user_input.conversation_id or ulid.ulid_now()
        model = settings[CONF_MODEL]
        intent_response = intent.IntentResponse(language=user_input.language)
        llm_api: llm.APIInstance | None = None
        tools: dict[str, dict[str, Any]] | None = None
        user_name: str | None = None
        llm_context = llm.LLMContext(
            platform=DOMAIN,
            context=user_input.context,
            user_prompt=user_input.text,
            language=user_input.language,
            assistant=conversation.DOMAIN,
            device_id=user_input.device_id,
        )

        _LOGGER.debug("CONF_LLM_HASS_API=%s", settings.get(CONF_LLM_HASS_API))
        if settings.get(CONF_LLM_HASS_API):
            try:
                llm_api = await llm.async_get_api(
                    self.hass,
                    settings[CONF_LLM_HASS_API],
                    llm_context,
                )
            except HomeAssistantError as err:
                _LOGGER.error("Error getting LLM API: %s", err)
                intent_response.async_set_error(
                    intent.IntentResponseErrorCode.UNKNOWN,
                    f"Error preparing LLM API: {err}",
                )
                return conversation.ConversationResult(
                    response=intent_response, conversation_id=user_input.conversation_id
                )
            tools = [
                _format_tool(tool, llm_api.custom_serializer) for tool in llm_api.tools
            ]
            _LOGGER.debug("tools=%s", tools)

        if (
            user_input.context
            and user_input.context.user_id
            and (
                user := await self.hass.auth.async_get_user(user_input.context.user_id)
            )
        ):
            user_name = user.name

        # Look up message history
        message_history: MessageHistory | None = None
        message_history = self._history.get(conversation_id)
        if message_history is None:
            # New history
            #
            # Render prompt and error out early if there's a problem
            try:
                prompt_parts = [
                    template.Template(
                        llm.BASE_PROMPT
                        + settings.get(CONF_PROMPT, llm.DEFAULT_INSTRUCTIONS_PROMPT),
                        self.hass,
                    ).async_render(
                        {
                            "ha_name": self.hass.config.location_name,
                            "user_name": user_name,
                            "llm_context": llm_context,
                        },
                        parse_result=False,
                    )
                ]
            except TemplateError as err:
                _LOGGER.error("Error rendering prompt: %s", err)
                intent_response.async_set_error(
                    intent.IntentResponseErrorCode.UNKNOWN,
                    f"Sorry, I had a problem generating my prompt: {err}",
                )
                return conversation.ConversationResult(
                    response=intent_response, conversation_id=conversation_id
                )

            if llm_api:
                _LOGGER.debug("llm api prompt parts")
                prompt_parts.append(llm_api.api_prompt)
            else:
                _LOGGER.debug("no llm api prompt parts")

            prompt = "\n".join(prompt_parts)
            _LOGGER.debug("Prompt: %s", prompt)

            message_history = MessageHistory(
                timestamp=time.monotonic(),
                messages=[
                    ollama.Message(role=MessageRole.SYSTEM.value, content=prompt)
                ],
            )
            self._history[conversation_id] = message_history
        else:
            # Bump timestamp so this conversation won't get cleaned up
            message_history.timestamp = time.monotonic()

        # Clean up old histories
        self._prune_old_histories()

        # Trim this message history to keep a maximum number of *user* messages
        max_messages = int(settings.get(CONF_MAX_HISTORY, DEFAULT_MAX_HISTORY))
        self._trim_history(message_history, max_messages)

        # Add new user message
        message_history.messages.append(
            ollama.Message(role=MessageRole.USER.value, content=user_input.text)
        )

        trace.async_conversation_trace_append(
            trace.ConversationTraceEventType.AGENT_DETAIL,
            {"messages": message_history.messages},
        )

        # Get response
        # To prevent infinite loops, we limit the number of iterations
        for _iteration in range(MAX_TOOL_ITERATIONS):
            try:
                response = await client.chat(
                    model=model,
                    # Make a copy of the messages because we mutate the list later
                    messages=list(message_history.messages),
                    stream=False,
                    tools=tools,
                    keep_alive=KEEP_ALIVE_FOREVER,
                )
            except (ollama.RequestError, ollama.ResponseError) as err:
                _LOGGER.error("Unexpected error talking to Ollama server: %s", err)
                intent_response.async_set_error(
                    intent.IntentResponseErrorCode.UNKNOWN,
                    f"Sorry, I had a problem talking to the Ollama server: {err}",
                )
                return conversation.ConversationResult(
                    response=intent_response, conversation_id=conversation_id
                )

            _LOGGER.debug("Response: %s", response)
            response_message = response["message"]
            tool_calls = response_message.get("tool_calls")

            def message_convert(response_message: Any) -> ollama.Message:
                msg = ollama.Message(role=response_message["role"])
                if content := response_message.get("content"):
                    msg["content"] = content
                if tool_calls := response_message.get("tool_calls"):
                    msg["tool_calls"] = tool_calls
                return msg

            message_history.messages.append(message_convert(response_message))

            if not tool_calls or not llm_api:
                _LOGGER.debug("tool_calls=%s", tool_calls)
                _LOGGER.debug("llm_api=%s", llm_api)
                break

            _LOGGER.debug("Response: %s", response_message.get("content"))
            _LOGGER.debug("Tools calls: %s", tool_calls)
            for tool_call in tool_calls:
                tool_input = llm.ToolInput(
                    tool_name=tool_call["function"]["name"],
                    tool_args=tool_call["function"]["arguments"],
                )
                _LOGGER.debug(
                    "Tool call: %s(%s)", tool_input.tool_name, tool_input.tool_args
                )

                try:
                    tool_response = await llm_api.async_call_tool(tool_input)
                except (HomeAssistantError, vol.Invalid) as e:
                    tool_response = {"error": type(e).__name__}
                    if str(e):
                        tool_response["error_text"] = str(e)

                _LOGGER.debug("Tool response: %s", tool_response)
                message_history.messages.append(
                    ollama.Message(role="tool", content=json.dumps(tool_response))
                )

        # Create intent response
        intent_response = intent.IntentResponse(language=user_input.language)
        intent_response.async_set_speech(response_message["content"])
        return conversation.ConversationResult(
            response=intent_response, conversation_id=conversation_id
        )

    def _prune_old_histories(self) -> None:
        """Remove old message histories."""
        now = time.monotonic()
        self._history = {
            conversation_id: message_history
            for conversation_id, message_history in self._history.items()
            if (now - message_history.timestamp) <= MAX_HISTORY_SECONDS
        }

    def _trim_history(self, message_history: MessageHistory, max_messages: int) -> None:
        """Trims excess messages from a single history."""
        if max_messages < 1:
            # Keep all messages
            return

        if message_history.num_user_messages >= max_messages:
            # Trim history but keep system prompt (first message).
            # Every other message should be an assistant message, so keep 2x
            # message objects.
            num_keep = 2 * max_messages
            drop_index = len(message_history.messages) - num_keep
            message_history.messages = [
                message_history.messages[0]
            ] + message_history.messages[drop_index:]
