from typing import TYPE_CHECKING, Any, Callable, ClassVar, Literal, Union

from pydantic import BaseModel, PrivateAttr, field_validator

from dynamiq.connections import BaseConnection, HttpApiKey
from dynamiq.nodes import ErrorHandling, NodeGroup
from dynamiq.nodes.node import ConnectionNode, ensure_config
from dynamiq.nodes.types import InferenceMode
from dynamiq.prompts import Prompt
from dynamiq.runnables import RunnableConfig

if TYPE_CHECKING:
    from litellm import CustomStreamWrapper, ModelResponse


class BaseLLMUsageData(BaseModel):
    """Model for LLM usage data.

    Attributes:
        prompt_tokens (int): Number of prompt tokens.
        prompt_tokens_cost_usd (float | None): Cost of prompt tokens in USD.
        completion_tokens (int): Number of completion tokens.
        completion_tokens_cost_usd (float | None): Cost of completion tokens in USD.
        total_tokens (int): Total number of tokens.
        total_tokens_cost_usd (float | None): Total cost of tokens in USD.
    """
    prompt_tokens: int
    prompt_tokens_cost_usd: float | None
    completion_tokens: int
    completion_tokens_cost_usd: float | None
    total_tokens: int
    total_tokens_cost_usd: float | None


class BaseLLM(ConnectionNode):
    """Base class for all LLM nodes.

    Attributes:
        MODEL_PREFIX (ClassVar[str | None]): Optional model prefix.
        name (str | None): Name of the LLM node. Defaults to "LLM".
        model (str): Model to use for the LLM.
        prompt (Prompt | None): Prompt to use for the LLM.
        connection (BaseConnection): Connection to use for the LLM.
        group (Literal[NodeGroup.LLMS]): Group for the node. Defaults to NodeGroup.LLMS.
        temperature (float): Temperature for the LLM. Defaults to 0.1.
        max_tokens (int): Maximum number of tokens for the LLM. Defaults to 1000.
        stop (list[str]): List of tokens to stop at for the LLM.
        error_handling (ErrorHandling): Error handling config. Defaults to ErrorHandling(timeout_seconds=600).
        top_p (float | None): Value to consider tokens with top_p probability.
        seed (int | None): Seed for generating the same result for repeated requests.
        presence_penalty (float | None): Penalize new tokens based on their existence in the text.
        frequency_penalty (float | None): Penalize new tokens based on their frequency in the text.
        tool_choice (str | None): Value to control which function is called by the model.
    """

    MODEL_PREFIX: ClassVar[str | None] = None
    name: str | None = "LLM"
    model: str
    prompt: Prompt | None = None
    connection: BaseConnection
    group: Literal[NodeGroup.LLMS] = NodeGroup.LLMS
    temperature: float = 0.1
    max_tokens: int = 1000
    stop: list[str] = None
    error_handling: ErrorHandling = ErrorHandling(timeout_seconds=600)
    top_p: float | None = None
    seed: int | None = None
    presence_penalty: float | None = None
    frequency_penalty: float | None = None
    tool_choice: str | None = None

    _completion: Callable = PrivateAttr()
    _stream_chunk_builder: Callable = PrivateAttr()

    @field_validator("model")
    @classmethod
    def set_model(cls, value: str | None) -> str:
        """Set the model with the appropriate prefix.

        Args:
            value (str | None): The model value.

        Returns:
            str: The model value with the prefix.
        """
        if cls.MODEL_PREFIX is not None and not value.startswith(cls.MODEL_PREFIX):
            value = f"{cls.MODEL_PREFIX}{value}"
        return value

    def __init__(self, **kwargs):
        """Initialize the BaseLLM instance.

        Args:
            **kwargs: Additional keyword arguments.
        """
        super().__init__(**kwargs)

        # Save a bit of loading time as litellm is slow
        from litellm import completion, stream_chunk_builder

        # Avoid the same imports multiple times and for future usage in execute
        self._completion = completion
        self._stream_chunk_builder = stream_chunk_builder

    @classmethod
    def get_usage_data(
        cls,
        model: str,
        completion: "ModelResponse",
    ) -> BaseLLMUsageData:
        """Get usage data for the LLM.

        This method generates usage data for the LLM based on the provided messages.

        Args:
            model (str): The model to use for generating the usage data.
            completion (ModelResponse): The completion response from the LLM.

        Returns:
            BaseLLMUsageData: A model containing the usage data for the LLM.
        """
        from litellm import cost_per_token

        usage = completion.model_extra["usage"]
        prompt_tokens = usage.prompt_tokens
        completion_tokens = usage.completion_tokens
        total_tokens = usage.total_tokens

        try:
            prompt_tokens_cost_usd, completion_tokens_cost_usd = cost_per_token(
                model=model, prompt_tokens=prompt_tokens, completion_tokens=completion_tokens
            )
            total_tokens_cost_usd = prompt_tokens_cost_usd + completion_tokens_cost_usd
        except Exception:
            prompt_tokens_cost_usd, completion_tokens_cost_usd, total_tokens_cost_usd = None, None, None

        return BaseLLMUsageData(
            prompt_tokens=prompt_tokens,
            prompt_tokens_cost_usd=prompt_tokens_cost_usd,
            completion_tokens=completion_tokens,
            completion_tokens_cost_usd=completion_tokens_cost_usd,
            total_tokens=total_tokens,
            total_tokens_cost_usd=total_tokens_cost_usd,
        )

    def _handle_completion_response(
        self,
        response: Union["ModelResponse", "CustomStreamWrapper"],
        config: RunnableConfig = None,
        **kwargs,
    ) -> dict:
        """Handle completion response.

        Args:
            response (ModelResponse | CustomStreamWrapper): The response from the LLM.
            config (RunnableConfig, optional): The configuration for the execution. Defaults to None.
            **kwargs: Additional keyword arguments.

        Returns:
            dict: A dictionary containing the generated content and tool calls.
        """
        content = response.choices[0].message.content
        if tool_calls := response.choices[0].message.tool_calls:
            tool_calls = [tc.model_dump() for tc in tool_calls]

        usage_data = self.get_usage_data(model=self.model, completion=response).model_dump()
        self.run_on_node_execute_run(callbacks=config.callbacks, usage_data=usage_data, **kwargs)

        return {"content": content, "tool_calls": tool_calls}

    def _handle_streaming_completion_response(
        self,
        response: Union["ModelResponse", "CustomStreamWrapper"],
        messages: list[dict],
        config: RunnableConfig = None,
        **kwargs,
    ):
        """Handle streaming completion response.

        Args:
            response (ModelResponse | CustomStreamWrapper): The response from the LLM.
            messages (list[dict]): The messages used for the LLM.
            config (RunnableConfig, optional): The configuration for the execution. Defaults to None.
            **kwargs: Additional keyword arguments.

        Returns:
            dict: A dictionary containing the generated content and tool calls.
        """
        chunks = []
        for chunk in response:
            chunks.append(chunk)

            self.run_on_node_execute_stream(
                config.callbacks,
                chunk.model_dump(),
                **kwargs,
            )

        full_response = self._stream_chunk_builder(chunks=chunks, messages=messages)
        return self._handle_completion_response(response=full_response, config=config, **kwargs)

    def execute(
        self,
        input_data: dict[str, Any],
        config: RunnableConfig = None,
        prompt: Prompt | None = None,
        schema: dict = {},
        inference_mode: InferenceMode = InferenceMode.DEFAULT,
        **kwargs,
    ):
        """Execute the LLM node.

        This method processes the input data, formats the prompt, and generates a response using
        the configured LLM.

        Args:
            input_data (dict[str, Any]): The input data for the LLM.
            config (RunnableConfig, optional): The configuration for the execution. Defaults to None.
            prompt (Prompt, optional): The prompt to use for this execution. Defaults to None.
            **kwargs: Additional keyword arguments.

        Returns:
            dict: A dictionary containing the generated content and tool calls.
        """
        config = ensure_config(config)
        prompt = prompt or self.prompt or Prompt(messages=[], tools=None)
        messages = prompt.format_messages(**input_data)
        tools = prompt.format_tools(**input_data)
        self.run_on_node_execute_run(callbacks=config.callbacks, prompt_messages=messages, **kwargs)

        # Use initialized client if it possible
        params = self.connection.conn_params
        if self.client and not isinstance(self.connection, HttpApiKey):
            params = {"client": self.client}

        response_format = None
        match inference_mode:
            case InferenceMode.STRUCTURED_OUTPUT:
                response_format = schema
            case InferenceMode.FUNCTION_CALLING:
                tools = schema

        response = self._completion(
            model=self.model,
            messages=messages,
            stream=self.streaming.enabled,
            temperature=self.temperature,
            max_tokens=self.max_tokens,
            tools=tools,
            tool_choice=self.tool_choice,
            stop=self.stop,
            top_p=self.top_p,
            seed=self.seed,
            presence_penalty=self.presence_penalty,
            frequency_penalty=self.frequency_penalty,
            response_format=response_format,
            **params,
        )

        handle_completion = (
            self._handle_streaming_completion_response if self.streaming.enabled else self._handle_completion_response
        )

        return handle_completion(response=response, messages=messages, config=config, input_data=input_data, **kwargs)