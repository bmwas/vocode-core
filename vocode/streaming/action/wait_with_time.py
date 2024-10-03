from typing import Type

from pydantic.v1 import BaseModel, Field
import asyncio

from vocode.streaming.action.base_action import BaseAction
from vocode.streaming.models.actions import ActionConfig as VocodeActionConfig
from vocode.streaming.models.actions import ActionInput, ActionOutput
from vocode.streaming.models.actions import FunctionCallActionTrigger

class WaitTimeVocodeActionConfig(VocodeActionConfig, type="action_wait_time"):  # type: ignore
    pass

class WaitTimeParameters(BaseModel):
    duration_seconds: float = Field(
        ...,
        description="The duration in seconds to wait before the agent responds."
    )
    upper_limit: float = Field(
        ...,
        description="The maximum duration in seconds the agent can wait."
    )

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
    parameters_type: Type[WaitTimeParameters] = WaitTimeParameters
    response_type: Type[WaitTimeResponse] = WaitTimeResponse

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
        # Enforce the upper limit on duration
        duration = min(action_input.params.duration_seconds, action_input.params.upper_limit)
        await asyncio.sleep(duration)
        return ActionOutput(
            action_type=action_input.action_config.type,
            response=WaitTimeResponse(success=True),
        )

# Usage Example
wait_with_time_action = WaitTimeVocodeActionConfig(
    type="action_wait_time",
    action_trigger=FunctionCallActionTrigger(
        type="action_trigger_function_call"
    )
)

wait_time_parameters = WaitTimeParameters(
    duration_seconds=5,
    upper_limit=4
)

action_input = ActionInput(
    action_config=wait_with_time_action,
    params=wait_time_parameters
)

