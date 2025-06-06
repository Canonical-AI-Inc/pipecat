#
# Copyright (c) 2024–2025, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

import copy
import json

from loguru import logger

from pipecat.frames.frames import (
    Frame,
    FunctionCallResultFrame,
    InterimTranscriptionFrame,
    LLMMessagesUpdateFrame,
    LLMSetToolsFrame,
    LLMTextFrame,
    TranscriptionFrame,
)
from pipecat.processors.aggregators.openai_llm_context import OpenAILLMContext
from pipecat.processors.frame_processor import FrameDirection
from pipecat.services.openai.llm import (
    OpenAIAssistantContextAggregator,
    OpenAIUserContextAggregator,
)

from . import events
from .frames import RealtimeFunctionCallResultFrame, RealtimeMessagesUpdateFrame


class OpenAIRealtimeLLMContext(OpenAILLMContext):
    def __init__(self, messages=None, tools=None, **kwargs):
        super().__init__(messages=messages, tools=tools, **kwargs)
        self.__setup_local()

    def __setup_local(self):
        self.llm_needs_settings_update = True
        self.llm_needs_initial_messages = True
        self._session_instructions = ""

        return

    @staticmethod
    def upgrade_to_realtime(obj: OpenAILLMContext) -> "OpenAIRealtimeLLMContext":
        if isinstance(obj, OpenAILLMContext) and not isinstance(obj, OpenAIRealtimeLLMContext):
            obj.__class__ = OpenAIRealtimeLLMContext
            obj.__setup_local()
        return obj

    # todo
    #   - finish implementing all frames

    def from_standard_message(self, message):
        if message.get("role") == "user":
            content = message.get("content")
            if isinstance(message.get("content"), list):
                content = ""
                for c in message.get("content"):
                    if c.get("type") == "text":
                        content += " " + c.get("text")
                    else:
                        logger.error(
                            f"Unhandled content type in context message: {c.get('type')} - {message}"
                        )
            return events.ConversationItem(
                role="user",
                type="message",
                content=[events.ItemContent(type="input_text", text=content)],
            )
        if message.get("role") == "assistant" and message.get("tool_calls"):
            tc = message.get("tool_calls")[0]
            return events.ConversationItem(
                type="function_call",
                call_id=tc["id"],
                name=tc["function"]["name"],
                arguments=tc["function"]["arguments"],
            )
        logger.error(f"Unhandled message type in from_standard_message: {message}")

    def get_messages_for_initializing_history(self):
        # We can't load a long conversation history into the openai realtime api yet. (The API/model
        # forgets that it can do audio, if you do a series of `conversation.item.create` calls.) So
        # our general strategy until this is fixed is just to put everything into a first "user"
        # message as a single input.
        if not self.messages:
            return []

        messages = copy.deepcopy(self.messages)

        # If we have a "system" message as our first message, let's pull that out into session
        # "instructions"
        if messages[0].get("role") == "system":
            self.llm_needs_settings_update = True
            system = messages.pop(0)
            content = system.get("content")
            if isinstance(content, str):
                self._session_instructions = content
            elif isinstance(content, list):
                self._session_instructions = content[0].get("text")
            if not messages:
                return []

        # If we have just a single "user" item, we can just send it normally
        if len(messages) == 1 and messages[0].get("role") == "user":
            return [self.from_standard_message(messages[0])]

        # Otherwise, let's pack everything into a single "user" message with a bit of
        # explanation for the LLM
        intro_text = """
        This is a previously saved conversation. Please treat this conversation history as a
        starting point for the current conversation."""

        trailing_text = """
        This is the end of the previously saved conversation. Please continue the conversation
        from here. If the last message is a user instruction or question, act on that instruction
        or answer the question. If the last message is an assistant response, simple say that you
        are ready to continue the conversation."""

        return [
            {
                "role": "user",
                "type": "message",
                "content": [
                    {
                        "type": "input_text",
                        "text": "\n\n".join(
                            [intro_text, json.dumps(messages, indent=2), trailing_text]
                        ),
                    }
                ],
            }
        ]

    def add_user_content_item_as_message(self, item):
        message = {
            "role": "user",
            "content": [{"type": "text", "text": item.content[0].transcript}],
        }
        self.add_message(message)


class OpenAIRealtimeUserContextAggregator(OpenAIUserContextAggregator):
    async def process_frame(
        self, frame: Frame, direction: FrameDirection = FrameDirection.DOWNSTREAM
    ):
        await super().process_frame(frame, direction)
        # Parent does not push LLMMessagesUpdateFrame. This ensures that in a typical pipeline,
        # messages are only processed by the user context aggregator, which is generally what we want. But
        # we also need to send new messages over the websocket, so the openai realtime API has them
        # in its context.
        if isinstance(frame, LLMMessagesUpdateFrame):
            await self.push_frame(RealtimeMessagesUpdateFrame(context=self._context))

        # Parent also doesn't push the LLMSetToolsFrame.
        if isinstance(frame, LLMSetToolsFrame):
            await self.push_frame(frame, direction)

    async def push_aggregation(self):
        # for the moment, ignore all user input coming into the pipeline.
        # todo: think about whether/how to fix this to allow for text input from
        #       upstream (transport/transcription, or other sources)
        pass


class OpenAIRealtimeAssistantContextAggregator(OpenAIAssistantContextAggregator):
    # The LLMAssistantContextAggregator uses TextFrames to aggregate the LLM output,
    # but the OpenAIRealtimeLLMService pushes LLMTextFrames and TTSTextFrames. We
    # need to override this proces_frame for LLMTextFrame, so that only the TTSTextFrames
    # are process. This ensures that the context gets only one set of messages.
    # OpenAIRealtimeLLMService also pushes TranscriptionFrames and InterimTranscriptionFrames,
    # so we need to ignore pushing those as well, as they're also TextFrames.
    async def process_frame(self, frame: Frame, direction: FrameDirection):
        if not isinstance(frame, (LLMTextFrame, TranscriptionFrame, InterimTranscriptionFrame)):
            await super().process_frame(frame, direction)

    async def handle_function_call_result(self, frame: FunctionCallResultFrame):
        await super().handle_function_call_result(frame)

        # The standard function callback code path pushes the FunctionCallResultFrame from the llm itself,
        # so we didn't have a chance to add the result to the openai realtime api context. Let's push a
        # special frame to do that.
        await self.push_frame(
            RealtimeFunctionCallResultFrame(result_frame=frame), FrameDirection.UPSTREAM
        )
