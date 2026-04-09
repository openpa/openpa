import json
import urllib.parse
from datetime import datetime
from typing import Any, AsyncGenerator, Dict, List, Optional, Set, Tuple, cast
import uuid
from openai.types.chat import ChatCompletionMessageParam
from openai.types.chat.chat_completion_tool_param import ChatCompletionToolParam

from app.agent.context_store import ReasoningContextStore
from app.remote_agents.routing_agent import RoutingAgent
from app.lib.llm.base import LLMProvider
from app.tools.intrinsic import get_intrinsic_tools, IntrinsicToolBehavior
from app.tools.intrinsic.user_notification import UserNotificationTool
from app.types import ReasoningStreamResponseType
from app.utils.logger import logger
from app.utils.common import find_similar_items, limit_messages, truncate_messages
from app.utils.event_parser import parse_agent_events
from app.utils.formatting import dict_to_text
from a2a.types import Part, TextPart
import jsonschema
from app.constants import MAX_TOKENS_FOR_HISTORY, MAX_TOKENS_PER_MESSAGE, ChatCompletionTypeEnum
from app.config.settings import BaseConfig
from app.lib.exception import AgentException

MAX_LLM_RETRIES = 2


template_instruction = '''(***[VERY IMPORTANT] You MUST always call the "personal_assistant_react" function tool to return your reasoning step. DO NOT return any plain text. Always use the function call with the fields: Thought, Action, Action_Input, Thought_In_Next.***)
{persona_description}
You will have these agents to support you. Your task is to respond to user commands by determining the next step you need to take by querying the relevant agent(s) until the goal is achieved, at which point you will return the Final Answer.
Please carefully consider the user's needs and construct a thoughtful plan to address them in the most intelligent way possible. You have access to the following tools below, Only use the necessary tools in the reasoning steps.
You must be cautious and think carefully about which tool to use and whether to use it at all.
Reasoning steps should avoid repeating previous reasoning to prevent redundant information and conserve resources. If you have sufficient information, proceed to the next step. Please detect if the Thought content across the steps is repeating itself. If it is, immediately modify the Thought content to avoid entering a loop.

More informations:
{more_informations}
Current time: {current_time}
Current Working Directory: {current_working_directory}

Tools:
{tools}

Use the following format:

Input: the input you must answer
Thought: you should always think about what to do
Action: the action to take from Thought, should be one of {tool_names}
Action_Input: the input sent to the action. For intrinsic tools, this is the response content to be sent to the user. Please respond in an easy-to-understand way, only text for the human listening (System information, technical specifications, and the device ID are hidden information)
Thought_In_Next: you should always think about what to do next after observing the results of the action
Observation: the result of the action
'''

template_input = '''
Begin!
{steps}
Thought:
'''

# Data structure for each reasoning step


class StepData:
    def __init__(
            self,
            input: Optional[str] = None,
            thought: Optional[str] = None,
            action: Optional[str] = None,
            action_input: Optional[str] = None,
            thought_in_next: Optional[str] = None,
            observation: Optional[list] = None,
            observation_text: Optional[str] = None):
        self.input = input
        self.thought = thought
        self.action = action
        self.action_input = action_input
        self.thought_in_next = thought_in_next
        self.observation = observation  # list[Part] — structured observation parts
        self.observation_text = observation_text  # flattened text for LLM prompt


class ReasoningAgent:
    def __init__(
            self,
            llm: LLMProvider,
            agents_info: dict,
            routing_agent: RoutingAgent,
            profile: str,
            context_id: Optional[str] = None,
            max_steps: int = 10,
            steps_length: int = 20,  # steps_length is always greater than or equal to max_steps
            metadata: Optional[Dict[str, Any]] = None):
        self.llm = llm
        # Filter out disabled agents from the tools/system prompt
        self.agents_info = {
            name: info for name, info in agents_info.items()
            if info.get('enabled', True)
        }
        self.agents_name = list(self.agents_info.keys())
        self.routing_agent = routing_agent
        self.profile = profile
        self.metadata = metadata or {}
        self.context_store = ReasoningContextStore()
        self.duplicate_detection = False
        self.steps: List[str] = []
        self.max_steps = max_steps
        self.steps_length = steps_length
        self.current_step_count = 0
        self.instruction = ""  # Will be set in run() method
        self.intrinsic_tools = get_intrinsic_tools()
        self.intrinsic_tool_names = [t.name for t in self.intrinsic_tools]
        self._intrinsic_tool_map = {t.name: t for t in self.intrinsic_tools}

        self._agent_call_history: List[Tuple[str, str]] = []
        # Token usage accumulators — tracks total tokens across all reasoning steps and remote agents
        self._total_input_tokens: int = 0
        self._total_output_tokens: int = 0
        self.reasoning_tool: ChatCompletionToolParam = {
            "type": "function",
            "function": {
                "name": "personal_assistant_react",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "Thought": {
                            "type": "string",
                        },
                        "Action": {
                            "type": "string",
                            "enum": [""],  # to be filled dynamically
                        },
                        "Action_Input": {
                            "type": "string",
                        },
                        "Thought_In_Next": {
                            "type": "string",
                        },
                    },
                    "required": ["Thought", "Action", "Action_Input", "Thought_In_Next"],
                    "additionalProperties": False
                }
            }
        }

        # Generate a new context_id if not provided
        if context_id is None:
            context_id = str(uuid.uuid4())
            logger.info(f"Generated new context_id: {context_id}")
        self.context_id = context_id

    async def _format_agents_info(self, agents_info: dict, agent_names: List[str]) -> List[str]:
        formatted_agents_info = []

        # Format intrinsic tools (same structure as remote agents)
        for tool in self.intrinsic_tools:
            tool_text = f"[{tool.name}]: {tool.description}\n"
            if tool.skills:
                tool_text += "*   Skills:\n"
                for skill in tool.skills:
                    examples_str = ", ".join([f"'{ex}'" for ex in skill.examples]) if skill.examples else ""
                    tool_text += f"    *   {skill.description}"
                    if examples_str:
                        tool_text += f", examples: {examples_str}"
                    tool_text += "\n"
            formatted_agents_info.append(tool_text)

        # Format remote agents
        for agent_name in agent_names:
            if agent_name in agents_info:
                agent_info = agents_info[agent_name]
                card = agent_info['card']

                agent_text = f"[{card.name}]: {card.description}\n"

                if card.skills:
                    agent_text += "*   Skills:\n"
                    for skill in card.skills:
                        examples_str = ", ".join([f"'{ex}'" for ex in skill.examples]) if skill.examples else ""
                        agent_text += f"    *   {skill.description}"
                        if examples_str:
                            agent_text += f", examples: {examples_str}"
                        agent_text += "\n"

                formatted_agents_info.append(agent_text)
        return formatted_agents_info

    async def run(self,
                  input: str,
                  history_messages: List[ChatCompletionMessageParam]) -> AsyncGenerator[ReasoningStreamResponseType,
                                                                                        None]:
        # Store history_messages for use in _loop (appended once, not per iteration)
        self.history_messages = history_messages
        # Reset agent call history for each new run
        self._agent_call_history = []

        # Check if we have existing context first
        context = self.context_store.get_context(self.context_id)
        # logger.debug(f"history_messages: {history_messages}")
        if context:
            logger.info(f"Loading existing context for context_id: {self.context_id}")
            stored_steps = context.get('steps') or []
            self.steps = stored_steps.copy()
            self.current_step_count = context.get('step_count', 0)

            # Regenerate instruction with merged agents and previous plan
            formatted_agents_info = await self._format_agents_info(self.agents_info, self.agents_name)
            persona_description = self.metadata.get("profile", {}).get(
                "persona_description", "You are a personal AI assistant")
            self.instruction = template_instruction.format(
                persona_description=persona_description,
                current_time=f"{datetime.now().isoformat()}",
                current_working_directory=BaseConfig.OPENPA_WORKING_DIR,
                more_informations=self._build_more_informations(),
                tools=''.join(formatted_agents_info),
                tool_names=", ".join(self.intrinsic_tool_names + self.agents_name))

            logger.info("Regenerated instruction with merged agents")

            # Append the new input and resume reasoning loop
            self._append_input_step(input)
            self._trim_steps()
            initial_step = StepData(input=input)
            async for item in self._loop(initial_step):
                yield item
            return
        else:
            logger.info(f"Creating new context for context_id: {self.context_id}")
            self.steps = []
            self.current_step_count = 0

        # Format agent information for instruction
        formatted_agents_info = await self._format_agents_info(self.agents_info, self.agents_name)

        persona_description = self.metadata.get("profile", {}).get(
            "persona_description", "You are a personal AI assistant")
        self.instruction = template_instruction.format(
            persona_description=persona_description,
            current_time=f"{datetime.now().isoformat()}",
            current_working_directory=BaseConfig.OPENPA_WORKING_DIR,
            more_informations=self._build_more_informations(),
            tools=''.join(formatted_agents_info),
            tool_names=", ".join(self.intrinsic_tool_names + self.agents_name))

        logger.info(f"instruction: {self.instruction}")
        initial_step = StepData(input=input)
        self._append_input_step(input)

        async for item in self._loop(initial_step):
            yield item

    async def _loop(
            self,
            step: StepData,
            llm_retry: int = 0) -> AsyncGenerator[ReasoningStreamResponseType, None]:
        # Check if we've exceeded max steps
        if self.current_step_count >= self.max_steps:
            logger.warning(f"Maximum reasoning steps ({self.max_steps}) reached. Terminating with current progress.")
            final_answer = f"I've reached the maximum number of reasoning steps ({
                self.max_steps}). Based on my analysis so far, here's what I can conclude: {
                step.thought if step.thought else 'Unable to complete the full reasoning process within the step limit.'}"
            self.steps.append(f"Thought: Maximum steps reached\nFinal_Answer: {final_answer}\n")
            self._trim_steps()
            logger.info("=== Final Answer (Max Steps Reached) ===")
            logger.info(final_answer)
            yield {
                "type": ChatCompletionTypeEnum.DONE,
                "data": final_answer,
                "input_tokens": self._total_input_tokens,
                "output_tokens": self._total_output_tokens,
            }
            return

        instruction = self.instruction

        self.current_step_count += 1

        input_section = template_input.format(steps='\n'.join(self.steps))
        logger.info(f"=== Reasoning Step ===\n{input_section}")

        # Dynamically update Action enum on the reasoning tool
        func_def = cast(dict, self.reasoning_tool["function"])
        func_def["parameters"]["properties"]["Action"]["enum"] = [
            ""] + self.agents_name + self.intrinsic_tool_names

        logger.debug(f"History messages before LLM call: {self.history_messages}")
        # Truncate older messages to MAX_TOKENS_PER_MESSAGE tokens each, then limit total history
        truncated_history = truncate_messages(self.history_messages, max_tokens_per_message=MAX_TOKENS_PER_MESSAGE)
        limited_history = limit_messages(truncated_history, max_length=MAX_TOKENS_FOR_HISTORY)
        messages: List[ChatCompletionMessageParam] = [
            {
                "role": "system",
                "content": instruction
            },
            *limited_history,
            {
                "role": "user",
                "content": input_section
            }
        ]

        final_messages_for_log = [msg for msg in messages if msg["role"] != "system"]
        logger.info(f"=== Final Messages for Reasoning Agent LLM ===\n{json.dumps(final_messages_for_log, indent=2)}")

        # Buffer all LLM responses first so we can extract token usage from the DONE chunk
        # before processing actions (FUNCTION_CALLING arrives before DONE in the stream,
        # but action handlers return early, so DONE would never be consumed otherwise).
        llm_responses = []
        try:
            async for response in self.llm.chat_completion(
                messages=messages,
                tools=[self.reasoning_tool],
                tool_choice="auto",
                temperature=1,
                reasoning_effort='low',
                max_tokens=32768,
                retry=3
            ):
                llm_responses.append(response)
        except AgentException as err:
            logger.error(
                f"LLM call failed at reasoning step {self.current_step_count} "
                f"(llm_retry={llm_retry}/{MAX_LLM_RETRIES}): {err}"
            )
            if llm_retry < MAX_LLM_RETRIES:
                # Undo the step count increment so the retry re-attempts the same step
                self.current_step_count -= 1
                async for retry_result in self._loop(step, llm_retry=llm_retry + 1):
                    yield retry_result
                return
            else:
                logger.error(
                    f"All LLM retries exhausted at reasoning step {self.current_step_count}. "
                    f"Yielding error fallback."
                )
                fallback = (
                    step.thought
                    or "I encountered an error processing your request. Please try again."
                )
                yield {
                    "type": ChatCompletionTypeEnum.CLARIFY,
                    "data": fallback,
                    "input_tokens": self._total_input_tokens,
                    "output_tokens": self._total_output_tokens,
                }
                return

        # Extract and accumulate token usage from the DONE chunk first
        finish_reason = None
        for response in llm_responses:
            if response["type"] == ChatCompletionTypeEnum.DONE:
                self._total_input_tokens += response.get("input_tokens") or 0
                self._total_output_tokens += response.get("output_tokens") or 0
                finish_reason = response.get("finish_reason")
                logger.info(
                    f"LLM token usage - input: {
                        response.get('input_tokens', 0)}, output: {
                        response.get('output_tokens', 0)}")
                logger.info(
                    f"Cumulative token usage - input: {self._total_input_tokens}, output: {self._total_output_tokens}")
                if finish_reason:
                    logger.info(f"LLM finish_reason: {finish_reason}")
                break

        # Track whether we received any actionable response
        has_result = False

        # Now process all responses with token usage already accumulated
        for response in llm_responses:
            if response["type"] == ChatCompletionTypeEnum.CONTENT:
                content = response.get("data")
                if not content:
                    logger.debug(f"CONTENT response with empty data, skipping. raw response: {response}")
                if content:
                    has_result = True
                    step = StepData(
                        thought=content,
                        action="Casual Chat Tool",
                        action_input=content,
                    )
                    logger.info("=== Casual Chat (from content) ===")

                    step_text = f"Thought: {step.thought}\nAction: Casual Chat Tool\nAction_Input: {content}\n"
                    self.steps.append(step_text)
                    self._trim_steps()

                    # Reset step count on intrinsic tool interruption
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
                logger.debug(f"FUNCTION_CALLING raw data: {response['data']}")
                if response["data"] and "function" in response["data"] and len(response["data"]["function"]) > 0:
                    response_data = response["data"]["function"][0]["arguments"]
                    if not response_data:
                        logger.warning(f"FUNCTION_CALLING has function entry but arguments is empty/None. "
                                       f"function entry: {response['data']['function'][0]}")
                    if response_data:
                        step = StepData(
                            thought=response_data.get('Thought', ''),
                            action=response_data.get('Action') or None,
                            action_input=response_data.get('Action_Input') or None,
                            thought_in_next=response_data.get('Thought_In_Next') or None,
                        )

                        # Handle intrinsic tools
                        intrinsic_tool = self._intrinsic_tool_map.get(step.action) if step.action else None
                        if intrinsic_tool:
                            has_result = True
                            result = await intrinsic_tool.run(
                                {"action_input": step.action_input or step.thought or ""})
                            response_text = result.content

                            step_text = f"Action: {step.action}\nAction_Input: {response_text}\n"
                            if step.thought_in_next:
                                if step.action == UserNotificationTool.TOOL_NAME:
                                    step_text += f"Thought_In_Next: I have already notified the user. In the next reasoning step, I will not use the '{UserNotificationTool.TOOL_NAME}' but will use other tools instead.\n"
                                else:
                                    step_text += f"Thought_In_Next: {step.thought_in_next}\n"
                            self.steps.append(step_text)
                            self._trim_steps()

                            if intrinsic_tool.behavior == IntrinsicToolBehavior.TERMINATE:
                                logger.info(f"=== {intrinsic_tool.name} (TERMINATE) ===")
                                if self.context_id:
                                    self._clear_context(self.context_id)
                                yield {
                                    "type": ChatCompletionTypeEnum.DONE,
                                    "data": response_text,
                                    "input_tokens": self._total_input_tokens,
                                    "output_tokens": self._total_output_tokens,
                                }
                                return
                            elif intrinsic_tool.behavior == IntrinsicToolBehavior.CLARIFY:
                                logger.info(f"=== {intrinsic_tool.name} (CLARIFY) ===")
                                self.current_step_count = 0
                                if self.context_id:
                                    self._save_context(self.context_id)
                                yield {
                                    "type": ChatCompletionTypeEnum.CLARIFY,
                                    "data": response_text,
                                    "input_tokens": self._total_input_tokens,
                                    "output_tokens": self._total_output_tokens,
                                }
                                return
                            elif intrinsic_tool.behavior == IntrinsicToolBehavior.CONTINUE:
                                logger.info(f"=== {intrinsic_tool.name} (CONTINUE) ===")
                                # Stream the output text to the client as main message content
                                # Append newline so each CONTINUE output is separated in the chat bubble
                                yield {
                                    "type": ChatCompletionTypeEnum.CONTENT,
                                    "data": response_text + "\n",
                                }
                                # Save context and continue the reasoning loop
                                if self.context_id:
                                    self._save_context(self.context_id)
                                # Continue recursion (same pattern as external agent calls)
                                async for recursive_result in self._loop(step):
                                    yield recursive_result
                                return

                        # External Agent calls
                        elif step.action in self.agents_name:
                            if step.action_input:
                                if self.duplicate_detection:
                                    # Duplicate agent call detection
                                    call_key = (cast(str, step.action), step.action_input)
                                    if call_key in self._agent_call_history:
                                        logger.warning(
                                            f"Duplicate agent call detected: {call_key}. Continuing with different reasoning direction.")
                                        # Add duplicate detection step and continue reasoning
                                        step_text = f"Thought: It appears the reasoning content has been repeated.\nAction: None\nAction_Input: None\nThought_In_Next: I will adjust my thought to avoid repetition and continue reasoning.\nObservation:\n```\nDetected duplicate agent call, adjusting reasoning to avoid loop.\n```\n"
                                        self.steps.append(step_text)
                                        self._trim_steps()

                                        # Save context and continue reasoning
                                        if self.context_id:
                                            self._save_context(self.context_id)

                                        # Continue recursion
                                        async for recursive_result in self._loop(step):
                                            yield recursive_result
                                        return
                                    self._agent_call_history.append(call_key)

                                # Check authentication status before calling remote agent
                                agent_name = cast(str, step.action)
                                remote_agent_info = self.agents_info.get(agent_name)
                                if remote_agent_info:
                                    remote_conn = remote_agent_info['remote_agent_connections']
                                    oauth_client = remote_conn.get_oauth_client_for_profile(self.profile)
                                    auth_status = oauth_client.get_auth_status()
                                    if auth_status in ("not_authenticated", "expired"):
                                        auth_action = "authenticate with" if auth_status == "not_authenticated" else "re-authenticate with"
                                        encoded_agent_name = urllib.parse.quote(agent_name)
                                        encoded_profile = urllib.parse.quote(self.profile)
                                        auth_link = (
                                            f"{BaseConfig.APP_URL}/dashboard/{encoded_agent_name}"
                                            f"/authenticate?profile={encoded_profile}&source=chat"
                                        )
                                        auth_message = (
                                            f"To access the data from {agent_name}, you need to {auth_action} this agent first. "
                                            f"Please go to the app to complete authentication for {agent_name} "
                                            f"or click this link to authenticate: [Authenticate {agent_name}]({auth_link})"
                                        )
                                        logger.info(
                                            f"Agent '{agent_name}' requires authentication "
                                            f"(status: {auth_status}). Requesting user authentication.")

                                        step_text = (
                                            f"Thought: {step.thought}\n"
                                            f"Action: {step.action}\n"
                                            f"Action_Input: {step.action_input}\n"
                                            f"Observation:\n```\nAuthentication required - agent '{agent_name}' is {auth_status}.\n```\n"
                                        )
                                        self.steps.append(step_text)
                                        self._trim_steps()

                                        self.current_step_count = 0

                                        if self.context_id:
                                            self._save_context(self.context_id)

                                        yield {
                                            "type": ChatCompletionTypeEnum.CLARIFY,
                                            "data": auth_message,
                                            "input_tokens": self._total_input_tokens,
                                            "output_tokens": self._total_output_tokens,
                                        }
                                        return

                                # Yield thinking artifact before calling external agent
                                yield {
                                    "type": ChatCompletionTypeEnum.THINKING_ARTIFACT,
                                    "data": {
                                        "Thought": step.thought,
                                        "Action": step.action,
                                        "Action_Input": step.action_input,
                                    },
                                }

                                events = []

                                # Validate client arguments against agent card schema
                                agent_info = self.agents_info.get(agent_name)
                                arguments_schema = agent_info.get('arguments_schema') if agent_info else None
                                client_arguments = self.metadata.get("arguments", {})
                                metadata_to_send = {}

                                if arguments_schema:
                                    # Filter to only include properties defined in the schema
                                    schema_properties = set(arguments_schema.get("properties", {}).keys())
                                    filtered_arguments = {
                                        k: v for k, v in client_arguments.items()
                                        if k in schema_properties
                                    }
                                    try:
                                        jsonschema.validate(instance=filtered_arguments, schema=arguments_schema)
                                        metadata_to_send = {"arguments": filtered_arguments}
                                    except jsonschema.ValidationError as e:
                                        error_field = ".".join(str(p) for p in e.absolute_path) if e.absolute_path else "root"
                                        observation_text = (
                                            f"Validation error for agent '{agent_name}' arguments: "
                                            f"field '{error_field}' - {e.message}. "
                                            f"Please ask the user to provide valid arguments."
                                        )
                                        step.observation = [Part(root=TextPart(text=observation_text))]
                                        step.observation_text = observation_text
                                        step_text = f"Thought: {step.thought}\nAction: {step.action}\nAction_Input: {step.action_input}\nObservation:\n```\n{observation_text}\n```\n"
                                        self.steps.append(step_text)
                                        self._trim_steps()
                                        if self.context_id:
                                            self._save_context(self.context_id)
                                        async for recursive_result in self._loop(step):
                                            yield recursive_result
                                        return
                                elif client_arguments:
                                    metadata_to_send = {"arguments": client_arguments}

                                logger.info(f"Calling agent '{step.action}' with input: {step.action_input}")
                                logger.info(f"Arguments for agent: {metadata_to_send} use profile: {self.profile}")
                                async for event in self.routing_agent.request(
                                    agent_name=agent_name,
                                    query=step.action_input,
                                    context_id=self.context_id,
                                    metadata=metadata_to_send,
                                    profile=self.profile,
                                ):
                                    events.append(event)
                                    yield {
                                        # Use a special type or repurpose THINK/CONTENT
                                        "type": ChatCompletionTypeEnum.STATUS_UPDATE,
                                        "data": event,
                                    }

                                # print events for debugging
                                logger.debug(f"Events from agent '{step.action}': {events}")
                                observation_text, remote_token_usage, observation_parts = parse_agent_events(events)
                                step.observation = observation_parts
                                step.observation_text = observation_text

                                # Accumulate remote agent token usage
                                if remote_token_usage:
                                    self._total_input_tokens += remote_token_usage.get("input_tokens", 0)
                                    self._total_output_tokens += remote_token_usage.get("output_tokens", 0)
                                    logger.info(
                                        f"Remote agent token usage - input: {
                                            remote_token_usage.get(
                                                'input_tokens', 0)}, output: {
                                            remote_token_usage.get(
                                                'output_tokens', 0)}")

                                # Yield result artifact after receiving observation
                                yield {
                                    "type": ChatCompletionTypeEnum.RESULT_ARTIFACT,
                                    "data": {
                                        "Observation": observation_parts,
                                    },
                                }

                            # Build step text for all decisions (specifically for continue cases here)
                            step_text = ""
                            step_text += f"Thought: {step.thought}\n" if step.thought else ""
                            step_text += f"Action: {step.action}\n" if step.action else ""
                            step_text += f"Action_Input: {step.action_input}\n" if step.action_input else ""
                            step_text += f"Thought_In_Next: {step.thought_in_next}\n" if step.thought_in_next else ""
                            step_text += f"Observation:\n```\n{step.observation_text}\n```\n" if step.observation_text else ""
                            self.steps.append(step_text)
                            self._trim_steps()

                            # Save context for Continue decision (which is now just the loop continuing)
                            if self.context_id:
                                self._save_context(self.context_id)

                            # Continue recursion
                            async for recursive_result in self._loop(step):
                                yield recursive_result
                            return  # Prevent processing remaining LLM stream events after recursion

                        # Unrecognized or empty action — fallback to casual chat
                        else:
                            has_result = True
                            logger.warning(f"Unrecognized action: '{step.action}'. Treating as casual chat.")
                            chat_response = step.action_input or step.thought or "I encountered an issue processing your request."
                            step_text = f"Thought: {step.thought}\nAction: Casual Chat Tool\nAction_Input: {chat_response}\n"
                            self.steps.append(step_text)
                            self._trim_steps()
                            # Reset step count on intrinsic tool interruption
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

                else:
                    logger.warning(
                        f"FUNCTION_CALLING response has invalid structure, skipping. "
                        f"data={response.get('data')}")

            elif response["type"] == ChatCompletionTypeEnum.DONE:
                # Token usage already accumulated in the pre-processing step above
                response_data = response.get('data')
                logger.info(f"Reasoning Response Data Done")

                # Check for truncation
                if finish_reason == "length" and not has_result:
                    logger.warning(
                        f"LLM output was truncated (finish_reason=length). Response likely exceeded max_tokens={
                            BaseConfig.MAX_REASONING_TOKENS}. " "No actionable response (CONTENT or FUNCTION_CALLING) was received.")

        # Safety fallback: if no branch yielded a result, check for truncation and provide detailed fallback
        if not has_result:
            if finish_reason == "length":
                logger.error(
                    f"Reasoning loop failed: LLM output was truncated (finish_reason=length). "
                    f"max_tokens={BaseConfig.MAX_REASONING_TOKENS} may be insufficient for this response. "
                    "Consider increasing MAX_REASONING_TOKENS or simplifying the prompt.")
                fallback = "I encountered an issue completing your request. The response was too long for the current configuration. Please try rephrasing your question or breaking it into smaller parts."
            else:
                logger.warning(
                    f"Reasoning loop ended without producing a result. finish_reason={finish_reason}, "
                    f"step.thought={step.thought[:100] if step.thought else None}")
                # Retry: LLM returned stop but no valid result — likely malformed response
                if llm_retry < MAX_LLM_RETRIES:
                    logger.warning(
                        f"Retrying reasoning step due to empty result "
                        f"(llm_retry={llm_retry + 1}/{MAX_LLM_RETRIES})")
                    self.current_step_count -= 1
                    async for retry_result in self._loop(step, llm_retry=llm_retry + 1):
                        yield retry_result
                    return
                fallback = step.thought or "I was unable to complete the reasoning process."
        else:
            fallback = step.thought or "I was unable to complete the reasoning process."

        logger.warning(f"Yielding fallback: {fallback[:100]}")
        yield {
            "type": ChatCompletionTypeEnum.CLARIFY,
            "data": fallback,
            "input_tokens": self._total_input_tokens,
            "output_tokens": self._total_output_tokens,
        }

    def _save_context(self, context_id: str) -> None:
        """Save the current reasoning context for later restoration."""
        self.context_store.save_context(
            context_id=context_id,
            steps=self.steps,
            step_count=self.current_step_count,
        )

    def _clear_context(self, context_id: str) -> None:
        """Clear the saved context after completion."""
        self.context_store.clear_context(context_id)

    def _trim_steps(self) -> None:
        """Trim the steps list to stay within steps_length.

        Preserves the first step (original user input) and evicts
        the oldest intermediate steps (from index 1) when the list
        exceeds the configured limit.
        """
        while len(self.steps) > self.steps_length and len(self.steps) > 1:
            self.steps.pop(1)

    def _build_more_informations(self) -> str:
        profile_info = self.metadata.get("profile", {})
        if not isinstance(profile_info, dict):
            return ""

        user_informations = profile_info.get("user_informations")
        if user_informations is None:
            # Fallback for old flat format
            excluded = {"userid", "persona_description"}
            user_informations = {k: v for k, v in profile_info.items() if k not in excluded}

        if not isinstance(user_informations, dict):
            return ""

        return dict_to_text(user_informations)

    def _append_input_step(self, user_input: str) -> None:
        """Append the latest user input to the serialized step history."""
        step_text = f"Input: {user_input}\n"
        self.steps.append(step_text)
        self._trim_steps()

    @classmethod
    def get_context(cls, context_id: str) -> Optional[Dict[str, Any]]:
        """Get a saved context by context_id (useful for debugging/inspection)."""
        store = ReasoningContextStore()
        return store.get_context(context_id)

    @classmethod
    def clear_all_contexts(cls) -> None:
        """Clear all saved contexts (useful for cleanup)."""
        store = ReasoningContextStore()
        store.clear_all_contexts()
