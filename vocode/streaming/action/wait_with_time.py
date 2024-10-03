from typing import Type

from pydantic.v1 import BaseModel, Field
import asyncio

from vocode.streaming.action.base_action import BaseAction
from vocode.streaming.models.actions import (
    ActionConfig as VocodeActionConfig,
    ActionInput,
    ActionOutput,
    FunctionCallActionTrigger,
)

class WaitTimeVocodeActionConfig(VocodeActionConfig, type="action_wait_time"):  # type: ignore
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
        None,
        WaitTimeResponse,
    ]
):
    description: str = (
        "Use this action to make the agent wait for a specified duration (in seconds). "
        "When the wait starts, the agent will inform the caller with an initial message. "
        "After the wait time expires, the agent will prompt the caller with a timeout message."
    )
    parameters_type: Type[None] = None
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

    async def run(self, action_input: ActionInput[None]) -> ActionOutput[WaitTimeResponse]:
        # Retrieve duration_seconds and upper_limit from action_config
        duration_seconds = self.action_config.duration_seconds
        upper_limit = self.action_config.upper_limit

        # Enforce the upper limit on duration
        duration = min(duration_seconds, upper_limit)
        print(">>>>>>>>> Upper Limit was >>>>>>>>>>>>>>>>>>>", upper_limit)
        print(">>>>>>>>> Duration captured was >>>>>>>>>>>>>>>>>>>", duration_seconds)
        print(">>>>>>>>> Wait duration is >>>>>>>>>>>>>>>>>>>", duration)

        await asyncio.sleep(duration)
        return ActionOutput(
            action_type=self.action_config.type,
            response=WaitTimeResponse(success=True),
        )
