from typing import Type, Union, Optional

from pydantic.v1 import BaseModel, Field
import asyncio

from vocode.streaming.action.base_action import BaseAction
from vocode.streaming.models.actions import (
    ActionConfig as VocodeActionConfig,
    ActionInput,
    ActionOutput,
    FunctionCallActionTrigger,
)

# Define the action configuration with optional parameters
class WaitTimeVocodeActionConfig(VocodeActionConfig, type="action_wait_time"):  # type: ignore
    duration_seconds: Optional[float] = Field(
        None,
        description="The duration in seconds to wait before the agent responds."
    )
    upper_limit: Optional[float] = Field(
        None,
        description="The maximum duration in seconds the agent can wait."
    )

    def get_duration_seconds(self, input: ActionInput) -> float:
        if isinstance(input.params, WaitTimeRequiredParameters):
            return input.params.duration_seconds
        elif isinstance(input.params, WaitTimeEmptyParameters):
            assert self.duration_seconds is not None, "duration_seconds must be provided"
            return self.duration_seconds
        else:
            raise TypeError("Invalid input params type")

    def get_upper_limit(self, input: ActionInput) -> float:
        if isinstance(input.params, WaitTimeRequiredParameters):
            return input.params.upper_limit
        elif isinstance(input.params, WaitTimeEmptyParameters):
            assert self.upper_limit is not None, "upper_limit must be provided"
            return self.upper_limit
        else:
            raise TypeError("Invalid input params type")

# Define the parameters classes
class WaitTimeEmptyParameters(BaseModel):
    pass

class WaitTimeRequiredParameters(BaseModel):
    duration_seconds: float = Field(
        ...,
        description="The duration in seconds to wait before the agent responds."
    )
    upper_limit: float = Field(
        ...,
        description="The maximum duration in seconds the agent can wait."
    )

WaitTimeParameters = Union[WaitTimeEmptyParameters, WaitTimeRequiredParameters]

class WaitTimeResponse(BaseModel):
    success: bool

class WaitTime(
    BaseAction[
        WaitTimeVocodeActionConfig,
        WaitTimeParameters,
        WaitTimeResponse,
    ]
):
    description: str = (
        "Use this action to make the agent wait for a specified duration (in seconds). "
        "When the wait starts, the agent will inform the caller with an initial message. "
        "After the wait time expires, the agent will prompt the caller with a timeout message."
    )
    response_type: Type[WaitTimeResponse] = WaitTimeResponse

    @property
    def parameters_type(self) -> Type[WaitTimeParameters]:
        if self.action_config.duration_seconds is not None and self.action_config.upper_limit is not None:
            return WaitTimeEmptyParameters
        else:
            return WaitTimeRequiredParameters

    def __init__(
        self,
        action_config: WaitTimeVocodeActionConfig,
    ):
        super().__init__(
            action_config,
            quiet=False,
            should_respond="always",
        )

    async def run(self, action_input: ActionInput[WaitTimeParameters]) -> ActionOutput[WaitTimeResponse]:
        # Retrieve duration_seconds and upper_limit
        duration_seconds = self.action_config.get_duration_seconds(action_input)
        upper_limit = self.action_config.get_upper_limit(action_input)

        # Enforce the upper limit on duration
        duration = min(duration_seconds, upper_limit)
        await asyncio.sleep(duration)
        return ActionOutput(
            action_type=action_input.action_config.type,
            response=WaitTimeResponse(success=True),
        )
