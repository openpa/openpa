"""Reasoning agent (ReAct loop) -- registry-driven, polymorphic dispatch.

The agent treats every capability uniformly through :class:`Tool`. The Action
enum is built from ``tool_id``s (which are guaranteed unique). When the LLM
selects an Action, the registry returns the corresponding :class:`Tool` and
``tool.execute(...)`` produces a :class:`ToolEvent` stream that drives:

- ``CONTENT`` streamed to the user  (CONTINUE behavior)
- DONE / CLARIFY responses           (TERMINATE / CLARIFY behaviors)
- Step-history observation appended  (OBSERVE behavior, default)
- Authentication redirects           (ToolErrorEvent with auth_required)

There is **no** per-type branching here -- the polymorphism is in the Tool
implementations. The reasoning agent only knows about Tool / ToolEvent.
"""

from __future__ import annotations

import json
import os
import platform
import uuid
from datetime import datetime
from typing import Any, AsyncGenerator, Dict, List, Optional, Tuple, cast

import jsonschema
from openai.types.chat import ChatCompletionMessageParam
from openai.types.chat.chat_completion_tool_param import ChatCompletionToolParam

from a2a.types import Part, TextPart

from app.agent.context_store import ReasoningContextStore
from app.config.settings import BaseConfig
from app.constants import MAX_TOKENS_FOR_HISTORY, MAX_TOKENS_PER_MESSAGE, ChatCompletionTypeEnum
from app.lib.exception import AgentException
from app.lib.llm.base import LLMProvider
from app.tools import (
    ToolBehavior,
    ToolErrorEvent,
    ToolRegistry,
    ToolResultEvent,
    ToolStatusEvent,
    ToolThinkingEvent,
    ToolType,
)
from app.tools.intrinsic import UserNotificationTool
from app.tools.ids import slugify
from app.skills.scanner import generate_dir_tree
from app.types import ReasoningStreamResponseType
from app.utils.common import limit_messages, truncate_messages
from app.utils.logger import logger
from app.utils.persona import read_persona_file


MAX_LLM_RETRIES = 2

OBSERVATION_START_MARKER = "------------OBS-START------------"
OBSERVATION_END_MARKER = "------------OBS-END------------"

template_instruction = '''(***[VERY IMPORTANT] You MUST always call the "personal_assistant_react" function tool to return your reasoning step. DO NOT return any plain text. Always use the function call with the fields: Thought, Action, Action_Input, Thought_In_Next.***)
{persona_description}
You will have these tools to support you. Your task is to respond to user commands by determining the next step you need to take by querying the relevant tool(s) until the goal is achieved, at which point you will return the Final Answer.
Please carefully consider the user's needs and construct a thoughtful plan to address them in the most intelligent way possible. You have access to the following tools below; only use the necessary tools in the reasoning steps.
You must be cautious and think carefully about which tool to use and whether to use it at all.
Reasoning steps should avoid repeating previous reasoning to prevent redundant information and conserve resources. If you have sufficient information, proceed to the next step. Please detect if the Thought content across the steps is repeating itself. If it is, immediately modify the Thought content to avoid entering a loop.

Current time: {current_time}
Current OS: {current_os}
Current Working Directory: `{current_working_directory}`
Current User Working Directory: `{current_user_working_directory}`
Current Skills Directory: `{current_skills_directory}`

Tools:
{tools}
Use the following format:

Input: the input you must answer
Thought: you should always think about what to do
Action: the action to take from Thought, should be one of {tool_names}
Action_Input: the input sent to the action. For intrinsic tools, this is the response content to be sent to the user. Please respond in an easy-to-understand way, only text for the human listening (System information, technical specifications, and the device ID are hidden information)
Thought_In_Next: you should always think about what to do next after observing the results of the action
Observation: the result of the action, the observation content will be placed between '{OBSERVATION_START_MARKER}' and '{OBSERVATION_END_MARKER}' to help you clearly identify the observation content.
{loaded_skills}'''

template_input = '''
Begin!
{steps}
Thought:
'''


_USER_NOTIFICATION_SLUG = slugify(UserNotificationTool.TOOL_NAME)


class StepData:
    def __init__(
        self,
        input: Optional[str] = None,
        thought: Optional[str] = None,
        action: Optional[str] = None,
        action_input: Optional[str] = None,
        thought_in_next: Optional[str] = None,
        observation: Optional[list] = None,
        observation_text: Optional[str] = None,
    ):
        self.input = input
        self.thought = thought
        self.action = action
        self.action_input = action_input
        self.thought_in_next = thought_in_next
        self.observation = observation
        self.observation_text = observation_text


def _format_arguments_section(arguments: dict) -> str:
    if not arguments:
        return ""
    lines = "*   Default Arguments:\n"
    for key, value in arguments.items():
        lines += f"    *   {key}: {value}\n"
    return lines


def _format_tool_for_prompt(tool, arguments: dict) -> str:
    """Render one tool block (id, description, default args, sub-skills)."""
    text = f"[{tool.tool_id}]: {tool.description}\n"
    text += _format_arguments_section(arguments)
    skills = tool.skills
    if skills:
        text += "*   Skills:\n"
        for skill in skills:
            examples_str = ", ".join([f"'{ex}'" for ex in skill.examples]) if skill.examples else ""
            text += f"    *   {skill.description}"
            if examples_str:
                text += f", examples: {examples_str}"
            text += "\n"
    return text


class ReasoningAgent:
    """ReAct loop driven by the unified :class:`Tool` registry."""

    def __init__(
        self,
        llm: LLMProvider,
        registry: ToolRegistry,
        profile: str,
        context_id: Optional[str] = None,
        max_steps: int = 40,
        steps_length: int = 80,
        reasoning: bool = True,
        allowed_skill_ids: Optional[set[str]] = None,
    ):
        self.llm = llm
        self.registry = registry
        self.profile = profile
        self.reasoning = reasoning

        # Snapshot the tool list available to this profile for this run.
        # When ``allowed_skill_ids`` is provided (automatic skill mode), skills
        # outside that set are hidden from both the Tools block and the Action
        # enum, so the LLM can only pick from the vector-retrieved top matches.
        tools = registry.tools_for_profile(profile)
        if allowed_skill_ids is not None:
            tools = [
                t for t in tools
                if t.tool_type is not ToolType.SKILL or t.tool_id in allowed_skill_ids
            ]
        self._tools = tools
        self._tools_by_id = {t.tool_id: t for t in self._tools}
        self._action_names = list(self._tools_by_id.keys())

        self.context_store = ReasoningContextStore()
        self.steps: List[str] = []
        self.max_steps = max_steps
        self.steps_length = steps_length
        self.current_step_count = 0
        self.instruction = ""
        self._agent_call_history: List[Tuple[str, str]] = []
        self._total_input_tokens = 0
        self._total_output_tokens = 0
        # Skills whose SKILL.md content has been folded into self.instruction.
        # Such skills are removed from the rendered Tools block and from the
        # Action enum so the LLM cannot re-invoke them. Cleared when the loop
        # ends via Casual Chat (CLARIFY) or Final Answer (TERMINATE).
        self._loaded_skill_ids: set[str] = set()
        self._loaded_skill_sections: Dict[str, str] = {}

        self.reasoning_tool: ChatCompletionToolParam = {
            "type": "function",
            "function": {
                "name": "personal_assistant_react",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "Thought": {"type": "string"},
                        "Action": {"type": "string", "enum": [""]},
                        "Action_Input": {"type": "string"},
                        "Thought_In_Next": {"type": "string"},
                    },
                    "required": ["Thought", "Action", "Action_Input", "Thought_In_Next"],
                    "additionalProperties": False,
                },
            },
        }

        if context_id is None:
            context_id = str(uuid.uuid4())
            logger.info(f"Generated new context_id: {context_id}")
        self.context_id = context_id

    # ── prompt building ───────────────────────────────────────────────

    def _build_tools_block(self) -> str:
        sections = []
        for tool in self._tools:
            if tool.tool_id in self._loaded_skill_ids:
                continue
            args = self._load_arguments(tool.tool_id) if tool.tool_type in (
                ToolType.A2A, ToolType.MCP, ToolType.BUILTIN,
            ) else {}
            sections.append(_format_tool_for_prompt(tool, args))
        return "".join(sections)

    def _build_loaded_skills_block(self) -> str:
        if not self._loaded_skill_sections:
            return ""
        parts = [
            "\n==========Start Skills Instructions==========\n"
            "You should only follow the instructions provided in the content loaded below. "
            "Do not use the **System File** tool to read the directory structure of `<skill_directory>` for security reasons."
            "Important: Your current working directory is the \"Current User Working Directory\", "
            "so to run any skill script in <skill_directory>/scripts, use the absolute path.\n"
        ]
        # If any loaded skill declares environment variables, remind the agent
        # to source the per-skill .env file before running any script -- exec_shell
        # is generic and won't preload them automatically.
        # if any(
        #     getattr(self._tools_by_id.get(tid), "environment_variables", None)
        #     for tid in self._loaded_skill_sections
        # ):
        #     parts.append(
        #         "Before running any script/app/binary in the scripts directory, "
        #         "you must export variables from the .env file so that the script/app/binary "
        #         "has the necessary environment variables to run.\n\n"
        #         "For Windows:\n"
        #         "Get-Content .env | ForEach-Object {\n"
        #         "    if ($_ -notmatch '^#|^\\s*$') {\n"
        #         "        $name, $value = $_ -split '=', 2\n"
        #         "        Set-Content \"env:\\$($name.Trim())\" $value.Trim()\n"
        #         "    }\n"
        #         "}\n\n"
        #         "For Linux and macOS:\n"
        #         "export $(grep -v '^#' .env | xargs)\n"
        #     )
        for tool_id, content in self._loaded_skill_sections.items():
            tool = self._tools_by_id.get(tool_id)
            display_name = tool.name if tool else tool_id
            parts.append(f"\n----- Skill: {display_name} -----\n{content}\n-------------------------\n")
        parts.append("==========End Skills Instructions==========\n")
        return "".join(parts)

    def _active_action_names(self) -> List[str]:
        return [name for name in self._action_names if name not in self._loaded_skill_ids]

    def _build_instruction(self) -> str:
        persona = read_persona_file(self.profile)
        return template_instruction.format(
            persona_description=persona,
            current_time=f"{datetime.now().isoformat()}",
            current_os=platform.system(),
            current_working_directory=BaseConfig.OPENPA_WORKING_DIR,
            current_skills_directory=os.path.join(BaseConfig.OPENPA_WORKING_DIR, self.profile, "skills"),
            current_user_working_directory=os.path.join(BaseConfig.OPENPA_WORKING_DIR, self.profile),
            tools=self._build_tools_block(),
            loaded_skills=self._build_loaded_skills_block(),
            tool_names=", ".join(self._active_action_names()),
            OBSERVATION_START_MARKER=OBSERVATION_START_MARKER,
            OBSERVATION_END_MARKER=OBSERVATION_END_MARKER,
        )

    # ── config lookups ────────────────────────────────────────────────

    def _load_arguments(self, tool_id: str) -> dict:
        try:
            return self.registry.config.get_arguments(tool_id, self.profile)
        except Exception:  # noqa: BLE001
            return {}

    def _load_variables(self, tool_id: str) -> dict[str, str]:
        try:
            return self.registry.config.get_variables(tool_id, self.profile, include_secrets=True)
        except Exception:  # noqa: BLE001
            return {}

    def _load_llm_params(self, tool_id: str) -> dict:
        try:
            return self.registry.config.get_llm_params(tool_id, self.profile)
        except Exception:  # noqa: BLE001
            return {}

    def _model_label_for(self, tool) -> str | None:
        if tool.tool_type in (ToolType.INTRINSIC, ToolType.SKILL):
            return self.llm.model_label
        if tool.tool_type == ToolType.A2A:
            return None
        if tool.tool_type in (ToolType.MCP, ToolType.BUILTIN):
            adapter = getattr(tool, "adapter", None)
            inner_llm = getattr(adapter, "_llm", None)
            return getattr(inner_llm, "model_label", None) if inner_llm else None
        return self.llm.model_label

    # ── main entry point ──────────────────────────────────────────────

    async def run(
        self,
        input: str,
        history_messages: List[ChatCompletionMessageParam],
    ) -> AsyncGenerator[ReasoningStreamResponseType, None]:
        self.history_messages = history_messages
        self._agent_call_history = []

        context = self.context_store.get_context(self.context_id)
        if context:
            logger.info(f"Loading existing context for context_id: {self.context_id}")
            stored_steps = context.get("steps") or []
            self.steps = stored_steps.copy()
            self.current_step_count = context.get("step_count", 0)
            self.instruction = self._build_instruction()
            self._append_input_step(input)
            self._trim_steps()
            initial_step = StepData(input=input)
            async for item in self._loop(initial_step):
                yield item
            return

        logger.info(f"Creating new context for context_id: {self.context_id}")
        self.steps = []
        self.current_step_count = 0
        self.instruction = self._build_instruction()
        logger.info(f"instruction: {self.instruction}")
        self._append_input_step(input)
        async for item in self._loop(StepData(input=input)):
            yield item

    # ── reasoning loop ────────────────────────────────────────────────

    async def _loop(
        self, step: StepData, llm_retry: int = 0,
    ) -> AsyncGenerator[ReasoningStreamResponseType, None]:
        if self.current_step_count >= self.max_steps:
            logger.warning(f"Max reasoning steps ({self.max_steps}) reached.")
            final_answer = (
                f"I've reached the maximum number of reasoning steps ({self.max_steps}). "
                f"Based on my analysis so far, here's what I can conclude: "
                f"{step.thought if step.thought else 'Unable to complete the full reasoning process within the step limit.'}"
            )
            self.steps.append(f"Thought: Maximum steps reached\nFinal_Answer: {final_answer}\n")
            self._trim_steps()
            yield {
                "type": ChatCompletionTypeEnum.DONE,
                "data": final_answer,
                "input_tokens": self._total_input_tokens,
                "output_tokens": self._total_output_tokens,
            }
            return

        self.current_step_count += 1
        input_section = template_input.format(steps="\n".join(self.steps))

        # Rebuild the system prompt every iteration so newly loaded skills are
        # appended (and re-invoked skills are filtered out of the Tools block).
        self.instruction = self._build_instruction()

        # logger.info(f"=== Instruction ===\n{self.instruction}")
        # logger.info(f"=== Reasoning Step ===\n{input_section}")

        # Action enum is the set of tool_ids, minus skills already folded into
        # the system prompt — this prevents the LLM from re-loading them.
        func_def = cast(dict, self.reasoning_tool["function"])
        func_def["parameters"]["properties"]["Action"]["enum"] = [""] + self._active_action_names()

        truncated = truncate_messages(self.history_messages, max_tokens_per_message=MAX_TOKENS_PER_MESSAGE)
        limited = limit_messages(truncated, max_length=MAX_TOKENS_FOR_HISTORY)
        messages: List[ChatCompletionMessageParam] = [
            {"role": "system", "content": self.instruction},
            *limited,
            {"role": "user", "content": input_section},
        ]

        llm_responses: list = []
        try:
            async for response in self.llm.chat_completion(
                messages=messages,
                tools=[self.reasoning_tool],
                tool_choice="auto",
                temperature=1,
                max_tokens=32768,
                retry=3,
            ):
                llm_responses.append(response)
        except AgentException as err:
            logger.error(f"LLM call failed at step {self.current_step_count}: {err}")
            if llm_retry < MAX_LLM_RETRIES:
                self.current_step_count -= 1
                async for r in self._loop(step, llm_retry + 1):
                    yield r
                return
            yield {
                "type": ChatCompletionTypeEnum.CLARIFY,
                "data": step.thought or "I encountered an error processing your request. Please try again.",
                "input_tokens": self._total_input_tokens,
                "output_tokens": self._total_output_tokens,
            }
            return

        # Token usage extraction
        finish_reason = None
        for response in llm_responses:
            if response["type"] == ChatCompletionTypeEnum.DONE:
                self._total_input_tokens += response.get("input_tokens") or 0
                self._total_output_tokens += response.get("output_tokens") or 0
                finish_reason = response.get("finish_reason")
                break

        has_result = False

        logger.info(f"LLM responses: {llm_responses}")
        for response in llm_responses:
            if response["type"] == ChatCompletionTypeEnum.CONTENT:
                content = response.get("data")
                if content:
                    has_result = True
                    fallback_id = self._first_tool_id_of(ToolType.INTRINSIC, name="Casual Chat Tool")
                    step = StepData(thought=content, action=fallback_id, action_input=content)
                    step_text = (
                        f"Thought: {step.thought}\n"
                        f"Action: {step.action}\nAction_Input: {content}\n"
                    )
                    self.steps.append(step_text)
                    self._trim_steps()
                    self.current_step_count = 0
                    if self.context_id:
                        self._save_context(self.context_id)
                    yield {
                        "type": ChatCompletionTypeEnum.CLARIFY,
                        "data": content,
                        "input_tokens": self._total_input_tokens,
                        "output_tokens": self._total_output_tokens,
                    }
                    return

            elif response["type"] == ChatCompletionTypeEnum.FUNCTION_CALLING:
                if not (response.get("data")
                        and "function" in response["data"]
                        and response["data"]["function"]):
                    continue
                response_data = response["data"]["function"][0]["arguments"]
                if not response_data:
                    continue

                step = StepData(
                    thought=response_data.get("Thought", ""),
                    action=response_data.get("Action") or None,
                    action_input=response_data.get("Action_Input") or None,
                    thought_in_next=response_data.get("Thought_In_Next") or None,
                )

                tool = self._tools_by_id.get(step.action) if step.action else None
                if tool is None:
                    has_result = True
                    chat_response = step.action_input or step.thought or "I encountered an issue processing your request."
                    fallback_id = self._first_tool_id_of(ToolType.INTRINSIC, name="Casual Chat Tool")
                    step_text = (
                        f"Thought: {step.thought}\n"
                        f"Action: {fallback_id}\nAction_Input: {chat_response}\n"
                    )
                    self.steps.append(step_text)
                    self._trim_steps()
                    self.current_step_count = 0
                    if self.context_id:
                        self._save_context(self.context_id)
                    yield {
                        "type": ChatCompletionTypeEnum.CLARIFY,
                        "data": chat_response,
                        "input_tokens": self._total_input_tokens,
                        "output_tokens": self._total_output_tokens,
                    }
                    return

                has_result = True
                async for item in self._dispatch(tool, step):
                    yield item
                return

        if finish_reason == "length":
            fallback = (
                "I encountered an issue completing your request. The response "
                "was too long for the current configuration."
            )
        else:
            if not has_result and llm_retry < MAX_LLM_RETRIES:
                self.current_step_count -= 1
                async for r in self._loop(step, llm_retry + 1):
                    yield r
                return
            fallback = step.thought or "I was unable to complete the reasoning process."

        yield {
            "type": ChatCompletionTypeEnum.CLARIFY,
            "data": fallback,
            "input_tokens": self._total_input_tokens,
            "output_tokens": self._total_output_tokens,
        }

    # ── polymorphic dispatch ──────────────────────────────────────────

    async def _dispatch(
        self, tool, step: StepData,
    ) -> AsyncGenerator[ReasoningStreamResponseType, None]:
        """Run ``tool`` and translate :class:`ToolEvent`s into the yield protocol."""

        # Validate / load arguments (only for tools that support them)
        arguments: dict = {}
        if tool.tool_type in (ToolType.A2A, ToolType.MCP, ToolType.BUILTIN):
            client_arguments = self._load_arguments(tool.tool_id)
            if tool.arguments_schema:
                schema_props = set(tool.arguments_schema.get("properties", {}).keys())
                filtered = {k: v for k, v in client_arguments.items() if k in schema_props}
                try:
                    jsonschema.validate(instance=filtered, schema=tool.arguments_schema)
                    arguments = filtered
                except jsonschema.ValidationError as e:
                    error_field = ".".join(str(p) for p in e.absolute_path) if e.absolute_path else "root"
                    obs = (
                        f"Validation error for tool '{tool.tool_id}' arguments: "
                        f"field '{error_field}' - {e.message}. "
                        f"Please ask the user to provide valid arguments."
                    )
                    step.observation = [Part(root=TextPart(text=obs))]
                    step.observation_text = obs
                    self._record_step_with_observation(step, obs)
                    if self.context_id:
                        self._save_context(self.context_id)
                    if not self.reasoning:
                        yield {
                            "type": ChatCompletionTypeEnum.CLARIFY,
                            "data": obs,
                            "input_tokens": self._total_input_tokens,
                            "output_tokens": self._total_output_tokens,
                        }
                        return
                    async for r in self._loop(step):
                        yield r
                    return
            else:
                arguments = client_arguments

        variables = self._load_variables(tool.tool_id) if tool.tool_type in (
            ToolType.A2A, ToolType.MCP, ToolType.BUILTIN,
        ) else {}
        llm_params = self._load_llm_params(tool.tool_id) if tool.tool_type in (
            ToolType.MCP, ToolType.BUILTIN,
        ) else {}

        # Refresh the tool's child LLM from current config BEFORE reading
        # Model_Label, so the Thinking Process shows the up-to-date model.
        if tool.tool_type in (ToolType.BUILTIN, ToolType.MCP):
            tool.refresh_llm(self.profile)

        # Yield a thinking artifact so the UI shows the tool invocation.
        # For skills, override the model-generated Action_Input with a concise
        # "Load skill <display name>" label so the Thinking Process clearly
        # reflects what is actually happening (the SKILL.md is being folded
        # into the system prompt, not passed any user-supplied input).
        if tool.tool_type is ToolType.SKILL:
            action_input = f"Load skill `{tool.name}`"
        else:
            action_input = step.action_input or step.thought or ""
        logger.info(f"Action input '{tool.tool_id}': {action_input}")
        thinking_payload = {
            "Thought": step.thought,
            "Action": tool.tool_id,
            "Action_Input": action_input,
            "Model_Label": self._model_label_for(tool),
            "Reasoning_Model_Label": self.llm.model_label,
        }
        if tool.tool_type is not ToolType.INTRINSIC:
            yield {"type": ChatCompletionTypeEnum.THINKING_ARTIFACT, "data": thinking_payload}
            
        # Drive the tool's event stream
        result_event: Optional[ToolResultEvent] = None
        error_event: Optional[ToolErrorEvent] = None
        try:
            if tool.tool_type is not ToolType.SKILL and tool.tool_type is not ToolType.INTRINSIC:
                tool_action_input = f"{step.action_input} (How would you think about this scenario in order to execute it: {step.thought})"
            else:
                tool_action_input = action_input
            
            logger.info(f"Tool action input '{tool.tool_id}': {tool_action_input}")
            async for event in tool.execute(
                query=tool_action_input,
                context_id=self.context_id,
                profile=self.profile,
                arguments=arguments,
                variables=variables,
                llm_params=llm_params,
            ):
                if isinstance(event, ToolThinkingEvent):
                    continue  # already emitted thinking artifact above
                if isinstance(event, ToolStatusEvent):
                    yield {"type": ChatCompletionTypeEnum.STATUS_UPDATE, "data": event.raw}
                    continue
                if isinstance(event, ToolResultEvent):
                    result_event = event
                    continue
                if isinstance(event, ToolErrorEvent):
                    error_event = event
                    break
        except Exception as e:  # noqa: BLE001
            logger.exception(f"Tool '{tool.tool_id}' raised during execute()")
            error_event = ToolErrorEvent(message=str(e))

        if error_event is not None:
            if error_event.auth_required:
                step.action = tool.tool_id
                self._record_step_with_observation(
                    step, f"Authentication required for tool '{tool.tool_id}'.",
                )
                self.current_step_count = 0
                if self.context_id:
                    self._save_context(self.context_id)
                yield {
                    "type": ChatCompletionTypeEnum.CLARIFY,
                    "data": error_event.message,
                    "input_tokens": self._total_input_tokens,
                    "output_tokens": self._total_output_tokens,
                }
                return
            obs = error_event.message
            step.observation = [Part(root=TextPart(text=obs))]
            step.observation_text = obs
            self._record_step_with_observation(step, obs)
            if self.context_id:
                self._save_context(self.context_id)
            if not self.reasoning:
                yield {
                    "type": ChatCompletionTypeEnum.CLARIFY,
                    "data": obs,
                    "input_tokens": self._total_input_tokens,
                    "output_tokens": self._total_output_tokens,
                }
                return
            async for r in self._loop(step):
                yield r
            return

        if result_event is None:
            yield {
                "type": ChatCompletionTypeEnum.CLARIFY,
                "data": step.thought or "I was unable to complete the reasoning process.",
                "input_tokens": self._total_input_tokens,
                "output_tokens": self._total_output_tokens,
            }
            return

        # Accumulate token usage
        if result_event.token_usage:
            self._total_input_tokens += result_event.token_usage.get("input_tokens", 0) or 0
            self._total_output_tokens += result_event.token_usage.get("output_tokens", 0) or 0

        behavior = result_event.behavior
        observation_text = result_event.observation_text
        observation_parts = result_event.observation_parts

        if behavior is ToolBehavior.TERMINATE:
            logger.info(f"=== {tool.tool_id} (TERMINATE) ===")
            self._record_action_step(step, observation_text)
            # Final Answer ends the run — drop any skills folded into the
            # system prompt so the next run starts with a fresh instruction.
            self._loaded_skill_ids.clear()
            self._loaded_skill_sections.clear()
            if self.context_id:
                self._clear_context(self.context_id)
            yield {
                "type": ChatCompletionTypeEnum.DONE,
                "data": observation_text,
                "input_tokens": self._total_input_tokens,
                "output_tokens": self._total_output_tokens,
            }
            return

        if behavior is ToolBehavior.CLARIFY:
            logger.info(f"=== {tool.tool_id} (CLARIFY) ===")
            self._record_action_step(step, observation_text)
            self.current_step_count = 0
            # Casual Chat ends the run — drop any skills folded into the
            # system prompt so the next run starts with a fresh instruction.
            self._loaded_skill_ids.clear()
            self._loaded_skill_sections.clear()
            if self.context_id:
                self._save_context(self.context_id)
            yield {
                "type": ChatCompletionTypeEnum.CLARIFY,
                "data": observation_text,
                "input_tokens": self._total_input_tokens,
                "output_tokens": self._total_output_tokens,
            }
            return

        if behavior is ToolBehavior.CONTINUE:
            logger.info(f"=== {tool.tool_id} (CONTINUE) ===")
            self._record_action_step(step, observation_text)
            yield {
                "type": ChatCompletionTypeEnum.CONTENT,
                "data": observation_text + "\n",
            }
            if self.context_id:
                self._save_context(self.context_id)
            if not self.reasoning:
                yield {
                    "type": ChatCompletionTypeEnum.DONE,
                    "data": "",
                    "input_tokens": self._total_input_tokens,
                    "output_tokens": self._total_output_tokens,
                }
                return
            async for r in self._loop(step):
                yield r
            return

        # OBSERVE (default for a2a/mcp/builtin/skill)
        logger.info(f"=== {tool.tool_id} (OBSERVE) ===")
        if tool.tool_type is ToolType.SKILL:
            # Skill content is treated as instruction: append SKILL.md to the
            # system prompt (rebuilt at the top of the next _loop iteration)
            # and remove the skill from the Tools block / Action enum so it
            # cannot be re-invoked and appended again.
            skill_text = getattr(tool.info, "full_content", "") or observation_text or ""  # type: ignore[attr-defined]
            dir_path = tool.info.dir_path  # type: ignore[attr-defined]
            tree_text = generate_dir_tree(dir_path)
            if tree_text:
                skill_text = f"Skill Directory Structure:\n```\n{tree_text}\n```\n\n{skill_text}"
            self._loaded_skill_sections[tool.tool_id] = skill_text
            self._loaded_skill_ids.add(tool.tool_id)
            # The full SKILL.md content is shown to the user in the frontend's
            # Thinking Process via RESULT_ARTIFACT, but the recorded step in
            # self.steps only carries a short pointer — the actual content
            # already lives in self.instruction, so the step history doesn't
            # need to duplicate it.
            skill_md_path = tool.info.dir_path / "SKILL.md"  # type: ignore[attr-defined]
            short_obs_text = f"[Skill loaded from {skill_md_path}]"
            short_obs_parts = [Part(root=TextPart(text=short_obs_text))]
            # Keep the recorded Action_Input in sync with the THINKING_ARTIFACT
            # ("Load skill `<name>`") rather than the raw model-generated input.
            step.action_input = action_input
            step.observation = short_obs_parts
            step.observation_text = short_obs_text
            yield {
                "type": ChatCompletionTypeEnum.RESULT_ARTIFACT,
                "data": {"Observation": observation_parts},
            }
            self._record_step_full(step, short_obs_text)
            if self.context_id:
                self._save_context(self.context_id)
            if not self.reasoning:
                async for r in self._finalize_without_reasoning(step, short_obs_text):
                    yield r
                return
            async for r in self._loop(step):
                yield r
            return
        step.observation = observation_parts
        step.observation_text = observation_text
        yield {
            "type": ChatCompletionTypeEnum.RESULT_ARTIFACT,
            "data": {"Observation": observation_parts},
        }
        self._record_step_full(step, observation_text)
        if self.context_id:
            self._save_context(self.context_id)
        if not self.reasoning:
            async for r in self._finalize_without_reasoning(step, observation_text):
                yield r
            return
        async for r in self._loop(step):
            yield r

    # ── single-step finalization (reasoning disabled) ──────────────────

    async def _finalize_without_reasoning(
        self, step: StepData, observation_text: str,
    ) -> AsyncGenerator[ReasoningStreamResponseType, None]:
        """Send tool result back to LLM as a tool-call output and get a final text answer."""

        messages: List[ChatCompletionMessageParam] = [
            {"role": "system", "content": "Please respond concisely based on the tool call content without including any explanation."},
            {"role": "user", "content": step.input or (self.steps[0].replace("Input: ", "").strip() if self.steps else "")},
        ]

        # Assistant message with tool_calls (the reasoning step that picked the tool)
        call_id = "call_0"
        messages.append({
            "role": "assistant",
            "tool_calls": [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {
                        "name": "tool_result",
                        "arguments": json.dumps({}),
                    },
                }
            ],
        })

        # Tool result message
        messages.append({
            "role": "tool",
            "tool_call_id": call_id,
            "content": observation_text or "No result",
        })

        logger.info(f"Messages for final LLM call: {messages}")

        # Second LLM call — no tools, just generate the final text answer
        content_parts: List[str] = []
        try:
            async for response in self.llm.chat_completion(
                messages=messages,
                tools=None,
                temperature=1,
            ):
                if response["type"] == ChatCompletionTypeEnum.CONTENT:
                    content = response.get("data")
                    if content:
                        content_parts.append(content)
                elif response["type"] == ChatCompletionTypeEnum.DONE:
                    self._total_input_tokens += response.get("input_tokens") or 0
                    self._total_output_tokens += response.get("output_tokens") or 0
                    break
        except AgentException as err:
            logger.error(f"Final LLM call failed in single-step mode: {err}")
            yield {
                "type": ChatCompletionTypeEnum.CLARIFY,
                "data": observation_text or "I encountered an error generating a response.",
                "input_tokens": self._total_input_tokens,
                "output_tokens": self._total_output_tokens,
            }
            return

        final_text = "".join(content_parts) if content_parts else observation_text or ""

        # Clear context (same as TERMINATE behavior)
        self._loaded_skill_ids.clear()
        self._loaded_skill_sections.clear()
        if self.context_id:
            self._clear_context(self.context_id)

        yield {
            "type": ChatCompletionTypeEnum.DONE,
            "data": final_text,
            "input_tokens": self._total_input_tokens,
            "output_tokens": self._total_output_tokens,
        }

    # ── step bookkeeping ──────────────────────────────────────────────

    def _first_tool_id_of(self, tool_type: ToolType, name: Optional[str] = None) -> str:
        for t in self._tools:
            if t.tool_type is tool_type and (name is None or t.name == name):
                return t.tool_id
        return name or ""

    def _record_action_step(self, step: StepData, response_text: str) -> None:
        step_text = f"Action: {step.action}\nAction_Input: {response_text}\n"
        if step.thought_in_next:
            if step.action == _USER_NOTIFICATION_SLUG:
                step_text += (
                    f"Thought_In_Next: I have already notified the user. In the next "
                    f"reasoning step, I will not use the '{step.action}' but will use "
                    f"other tools instead.\n"
                )
            else:
                step_text += f"Thought_In_Next: {step.thought_in_next}\n"
        self.steps.append(step_text)
        self._trim_steps()

    def _record_step_with_observation(self, step: StepData, observation_text: str) -> None:
        step_text = (
            (f"Thought: {step.thought}\n" if step.thought else "")
            + (f"Action: {step.action}\n" if step.action else "")
            + (f"Action_Input: {step.action_input}\n" if step.action_input else "")
            + (
                f"Observation:\n{OBSERVATION_START_MARKER}\n{observation_text}\n{OBSERVATION_END_MARKER}\n"
                if observation_text else ""
            )
        )
        self.steps.append(step_text)
        self._trim_steps()

    def _record_step_full(self, step: StepData, observation_text: str) -> None:
        step_text = (
            (f"Thought: {step.thought}\n" if step.thought else "")
            + (f"Action: {step.action}\n" if step.action else "")
            + (f"Action_Input: {step.action_input}\n" if step.action_input else "")
            + (f"Thought_In_Next: {step.thought_in_next}\n" if step.thought_in_next else "")
            + (
                f"Observation:\n{OBSERVATION_START_MARKER}\n{observation_text}\n{OBSERVATION_END_MARKER}\n"
                if observation_text else ""
            )
        )
        self.steps.append(step_text)
        self._trim_steps()

    def _save_context(self, context_id: str) -> None:
        self.context_store.save_context(
            context_id=context_id, steps=self.steps, step_count=self.current_step_count,
        )

    def _clear_context(self, context_id: str) -> None:
        self.context_store.clear_context(context_id)

    def _trim_steps(self) -> None:
        while len(self.steps) > self.steps_length and len(self.steps) > 1:
            self.steps.pop(1)

    def _append_input_step(self, user_input: str) -> None:
        self.steps.append(f"Input: {user_input}\n")
        self._trim_steps()

    @classmethod
    def get_context(cls, context_id: str) -> Optional[Dict[str, Any]]:
        store = ReasoningContextStore()
        return store.get_context(context_id)

    @classmethod
    def clear_all_contexts(cls) -> None:
        """Clear all saved contexts (useful for cleanup)."""
        store = ReasoningContextStore()
        store.clear_all_contexts()
