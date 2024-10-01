import json
import re
import textwrap
from datetime import datetime
from enum import Enum
from typing import Any, Callable, ClassVar

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr

from dynamiq.connections.managers import ConnectionManager
from dynamiq.nodes import ErrorHandling, Node, NodeGroup
from dynamiq.nodes.agents.exceptions import (
    ActionParsingException,
    AgentUnknownToolException,
    InvalidActionException,
    ToolExecutionException,
)
from dynamiq.nodes.node import NodeDependency, ensure_config
from dynamiq.prompts import Message, Prompt
from dynamiq.runnables import RunnableConfig, RunnableStatus
from dynamiq.types.streaming import StreamingConfig
from dynamiq.utils.logger import logger


class AgentStatus(Enum):
    """Represents the status of an agent's execution."""

    SUCCESS = "success"
    FAIL = "fail"


class AgentIntermediateStepModelObservation(BaseModel):
    initial: str | dict | None = None
    tool_using: str | dict | None = None
    tool_input: str | dict | None = None
    tool_output: Any = None
    updated: str | dict | None = None


class AgentIntermediateStep(BaseModel):
    input_data: str | dict
    model_observation: AgentIntermediateStepModelObservation
    final_answer: str | dict | None = None


class Agent(Node):
    """Base class for an AI Agent that interacts with a Language Model and tools."""

    DEFAULT_INTRODUCTION: ClassVar[str] = (
        "You are a helpful AI assistant designed to help with various tasks."
    )
    DEFAULT_DATE: ClassVar[str] = datetime.now().strftime("%d %B %Y")

    llm: Node = Field(..., description="Language Model (LLM) used by the agent.")
    group: NodeGroup = NodeGroup.AGENTS
    error_handling: ErrorHandling = ErrorHandling(timeout_seconds=600)
    streaming: StreamingConfig = StreamingConfig()
    tools: list[Node] = []
    name: str = "AI Agent"
    role: str | None = None
    goal: str | None = None
    max_loops: int = 1

    _prompt_blocks: dict[str, str] = PrivateAttr(default_factory=dict)
    _prompt_variables: dict[str, Any] = PrivateAttr(default_factory=dict)

    model_config = ConfigDict(arbitrary_types_allowed=True)

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._intermediate_steps: dict[int, dict] = {}
        self._run_depends: list[dict] = []
        self._init_prompt_blocks()

    @property
    def to_dict_exclude_params(self):
        return super().to_dict_exclude_params | {"llm": True, "tools": True}

    def to_dict(self, **kwargs) -> dict:
        """Converts the instance to a dictionary."""
        data = super().to_dict(**kwargs)
        data["llm"] = self.llm.to_dict(**kwargs)
        data["tools"] = [tool.to_dict(**kwargs) for tool in self.tools]
        return data

    def init_components(self, connection_manager: ConnectionManager = ConnectionManager()):
        """Initialize components for the manager and agents."""
        super().init_components(connection_manager)
        if self.llm.is_postponed_component_init:
            self.llm.init_components(connection_manager)

        for tool in self.tools:
            if tool.is_postponed_component_init:
                tool.init_components(connection_manager)
            tool.is_optimized_for_agents = True

    def _init_prompt_blocks(self):
        """Initializes default prompt blocks and variables."""
        self._prompt_blocks = {
            "introduction": self.DEFAULT_INTRODUCTION,
            "role": self.role or "",
            "goal": self.goal or "",
            "date": self.DEFAULT_DATE,
            "tools": "{tool_description}",
            "instructions": "",
            "output_format": "Provide your answer in a clear and concise manner.",
            "request": "User request: {input}",
            "context": "",
        }
        self._prompt_variables = {
            "tool_description": self.tool_description,
            "user_input": "",
        }

    def add_block(self, block_name: str, content: str):
        """Adds or updates a prompt block."""
        self._prompt_blocks[block_name] = content

    def set_prompt_variable(self, variable_name: str, value: Any):
        """Sets or updates a prompt variable."""
        self._prompt_variables[variable_name] = value

    def execute(
        self, input_data: dict[str, Any], config: RunnableConfig | None = None, **kwargs
    ) -> dict[str, Any]:
        """
        Executes the agent with the given input data.
        """
        logger.debug(f"Agent {self.name} - {self.id}: started with input {input_data}")
        self.reset_run_state()
        config = ensure_config(config)
        self.run_on_node_execute_run(config.callbacks, **kwargs)

        self._prompt_variables.update(input_data)
        kwargs = kwargs | {"parent_run_id": kwargs.get("run_id")}
        kwargs.pop("run_depends", None)

        result = self._run_agent(config=config, **kwargs)

        execution_result = {
            "content": result,
            "intermediate_steps": self._intermediate_steps,
        }

        if self.streaming.enabled:
            self.run_on_node_execute_stream(
                config.callbacks, execution_result, **kwargs
            )

        logger.debug(f"Agent {self.name} - {self.id}: finished with result {result}")
        return execution_result

    def _run_llm(self, prompt: str, config: RunnableConfig | None = None, **kwargs) -> str:
        """Runs the LLM with a given prompt and returns the result."""
        logger.debug(
            f"Agent {self.name} - {self.id}: Running LLM with prompt:\n{prompt}"
        )
        try:
            llm_result = self.llm.run(
                input_data={},
                config=config,
                prompt=Prompt(messages=[Message(role="user", content=prompt)]),
                run_depends=self._run_depends,
                **kwargs,
            )
            self._run_depends = [NodeDependency(node=self.llm).to_dict()]
            logger.debug(
                f"Agent {self.name} - {self.id}: RAW LLM result:\n{llm_result.output['content']}"
            )
            if llm_result.status != RunnableStatus.SUCCESS:
                raise ValueError("LLM execution failed")
            return llm_result.output["content"]
        except Exception as e:
            logger.error(
                f"Agent {self.name} - {self.id}: LLM execution failed: {str(e)}"
            )
            raise

    def _run_agent(self, config: RunnableConfig | None = None, **kwargs) -> str:
        """Runs the agent with the generated prompt and handles exceptions."""
        formatted_prompt = self.generate_prompt()
        try:
            return self._run_llm(formatted_prompt, config=config, **kwargs)
        except Exception as e:
            logger.error(f"Agent {self.name} - {self.id}: failed with error: {str(e)}")
            raise e

    def _parse_action(self, output: str) -> tuple[str | None, str | None]:
        """Parses the action and its input from the output string."""
        try:
            action_match = re.search(
                r"Action:\s*(.*?)\nAction Input:\s*(({\n)?.*?)(?:[^}]*$)",
                output,
                re.DOTALL,
            )
            if action_match:
                action = action_match.group(1).strip()
                action_input = action_match.group(2).strip()
                if "```json" in action_input:
                    action_input = action_input.replace("```json", "").replace("```", "").strip()

                action_input = json.loads(action_input)
                return action, action_input
            else:
                raise ActionParsingException()
        except Exception as e:
            logger.error(f"Error parsing action: {e}")
            raise ActionParsingException(
                (
                    "Error: Could not parse action and action input. "
                    "Please rewrite in the appropriate Action/Action Input "
                    "format with action input as a valid dictionary "
                    "Make sure all quotes are present."
                ),
                recoverable=True,
            )

    def _extract_final_answer(self, output: str) -> str:
        """Extracts the final answer from the output string."""
        match = re.search(r"Answer:\s*(.*)", output, re.DOTALL)
        return match.group(1).strip() if match else ""

    def _get_tool(self, action: str) -> Node:
        """Retrieves the tool corresponding to the given action."""
        tool = self.tool_by_names.get(action)
        if not tool:
            raise AgentUnknownToolException(
                f"Unknown tool: {action} Use only available tools and provide only its name in action field.\
                 Do not provide any aditional reasoning in action field.\
                  Reiterate and provide proper value for action field or say that you cannot answer the question."
            )
        return tool

    def _run_tool(self, tool: Node, tool_input: str, config, **kwargs) -> Any:
        """Runs a specific tool with the given input."""
        logger.debug(f"Agent {self.name} - {self.id}: Running tool '{tool.name}'")

        tool_result = tool.run(
            input_data=tool_input,
            config=config,
            run_depends=self._run_depends,
            **kwargs,
        )
        self._run_depends = [NodeDependency(node=tool).to_dict()]
        if tool_result.status != RunnableStatus.SUCCESS:
            logger.error({tool_result.output["content"]})
            if tool_result.output["recoverable"]:
                raise ToolExecutionException({tool_result.output["content"]})
            else:
                raise ValueError({tool_result.output["content"]})
        return tool_result.output["content"]

    @property
    def tool_description(self) -> str:
        """Returns a description of the tools available to the agent."""
        return (
            "\n".join(
                [f"{tool.name}: {tool.description.strip()}" for tool in self.tools]
            )
            if self.tools
            else ""
        )

    @property
    def tool_names(self) -> str:
        """Returns a comma-separated list of tool names available to the agent."""
        return ",".join([tool.name for tool in self.tools]) if self.tools else ""

    @property
    def tool_by_names(self) -> dict[str, Node]:
        """Returns a dictionary mapping tool names to their corresponding Node objects."""
        return {tool.name: tool for tool in self.tools} if self.tools else {}

    def reset_run_state(self):
        """Resets the agent's run state."""
        self._intermediate_steps = {}
        self._run_depends = []

    def generate_prompt(self, block_names: list[str] | None = None, **kwargs) -> str:
        """Generates the prompt using specified blocks and variables."""
        temp_variables = self._prompt_variables.copy()
        temp_variables.update(kwargs)
        prompt = ""
        for block, content in self._prompt_blocks.items():
            if block_names is None or block in block_names:
                if content:
                    formatted_content = content.format(**temp_variables)
                    prompt += f"{block.upper()}:\n{formatted_content}\n\n"

        prompt = textwrap.dedent(prompt)
        # Split into lines, strip each line, and then join with a single newline
        lines = prompt.splitlines()
        stripped_lines = [
            line.strip() for line in lines if line.strip()
        ]  # Remove empty lines if you want to avoid multiple newlines
        prompt = "\n".join(stripped_lines)
        # Remove redundant spaces between words in each line
        prompt = "\n".join(" ".join(line.split()) for line in prompt.split("\n"))
        return prompt


class AgentManager(Agent):
    """Manager class that extends the Agent class to include specific actions."""

    _actions: dict[str, Callable] = PrivateAttr(default_factory=dict)
    name: str = "Manager Agent"

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._init_actions()

    def to_dict(self, **kwargs) -> dict:
        """Converts the instance to a dictionary."""
        data = super().to_dict(**kwargs)
        data["_actions"] = {
            k: getattr(action, "__name__", str(action))
            for k, action in self._actions.items()
        }
        return data

    def _init_actions(self):
        """Initializes the default actions for the manager."""
        self._actions = {"plan": self._plan, "assign": self._assign, "final": self._final}

    def add_action(self, name: str, action: Callable):
        """Adds a custom action to the manager."""
        self._actions[name] = action

    def execute(
        self, input_data: dict[str, Any], config: RunnableConfig | None = None, **kwargs
    ) -> dict[str, Any]:
        """Executes the manager agent with the given input data and action."""
        self.reset_run_state()
        config = config or RunnableConfig()
        self.run_on_node_execute_run(config.callbacks, **kwargs)
        logger.info(
            f"AgentManager {self.name} - {self.id}: started with input {input_data}"
        )

        action = input_data.get("action")
        if not action or action not in self._actions:
            raise InvalidActionException(
                f"Invalid or missing action: {action}. Please choose action from {self._actions}"
            )

        self._prompt_variables.update(input_data)

        kwargs = kwargs | {"parent_run_id": kwargs.get("run_id")}
        kwargs.pop("run_depends", None)

        _result_llm = self._actions[action](config=config, **kwargs)
        result = {"action": action, "result": _result_llm}

        execution_result = {
            "content": result,
            "intermediate_steps": self._intermediate_steps,
        }

        if self.streaming.enabled:
            self.run_on_node_execute_stream(
                callbacks=config.callbacks,
                chunk=execution_result,
                wf_run_id=config.run_id,
                **kwargs,
            )

        logger.debug(
            f"AgentManager {self.name} - {self.id}: finished with result {result}"
        )
        return execution_result

    def _plan(self, config: RunnableConfig, **kwargs) -> str:
        """Executes the 'plan' action."""
        prompt = self.generate_prompt(block_names=["plan"])
        return self._run_llm(prompt, config, **kwargs)

    def _assign(self, config: RunnableConfig, **kwargs) -> str:
        """Executes the 'assign' action."""
        prompt = self.generate_prompt(block_names=["assign"])
        return self._run_llm(prompt, config, **kwargs)

    def _final(self, config: RunnableConfig, **kwargs) -> str:
        """Executes the 'final' action."""
        prompt = self.generate_prompt(block_names=["final"])
        return self._run_llm(prompt, config, **kwargs)