import datetime
import pytz
from typing import Optional, Type

from pydantic.v1 import BaseModel, Field
from vocode.streaming.action.base_action import BaseAction
from vocode.streaming.models.actions import ActionConfig, ActionInput, ActionOutput


class DetermineTimeOfDayVocodeActionConfig(ActionConfig, type="action_determine_time_of_day"):  # type: ignore
    pass


class DetermineTimeOfDayParameters(BaseModel):
    descriptor: str = Field(
        "Determines the time of day (morning, afternoon, evening, night) based on server time (America/Chicago)."
    )


class DetermineTimeOfDayResponse(BaseModel):
    success: bool
    time_of_day: Optional[str] = None
    message: Optional[str] = None


class DetermineTimeOfDay(
    BaseAction[
        DetermineTimeOfDayVocodeActionConfig,
        DetermineTimeOfDayParameters,
        DetermineTimeOfDayResponse,
    ]
):
    description: str = """
Determine the time of day (morning, afternoon, evening, night) based on the current server time in the America/Chicago timezone.
Called at the start of a call or at any point in the call as necessary
Requirements:
- **time_of_day**: A string representing the current time of day, one of "morning", "afternoon", "evening", or "night".
Instructions:
1. **Retrieve the current time** in the America/Chicago timezone.
2. **Determine the time of day** based on the hour:
   - Morning: 6:00 AM - 11:59 AM
   - Afternoon: 12:00 PM - 5:59 PM
   - Evening: 6:00 PM - 9:59 PM
   - Night: 10:00 PM - 5:59 AM
3. **Return** the `time_of_day` in the response.
"""

    parameters_type: Type[DetermineTimeOfDayParameters] = DetermineTimeOfDayParameters
    response_type: Type[DetermineTimeOfDayResponse] = DetermineTimeOfDayResponse

    def __init__(
        self,
        action_config: DetermineTimeOfDayVocodeActionConfig,
    ):
        super().__init__(
            action_config,
            quiet=False,
            should_respond="never",
        )

    async def run(
        self, action_input: ActionInput[DetermineTimeOfDayParameters]
    ) -> ActionOutput[DetermineTimeOfDayResponse]:
        # Get current time in America/Chicago timezone
        chicago_tz = pytz.timezone('America/Chicago')
        now = datetime.datetime.now(chicago_tz)
        hour = now.hour

        # Determine time of day
        if 6 <= hour < 12:
            time_of_day = "morning"
        elif 12 <= hour < 18:
            time_of_day = "afternoon"
        elif 18 <= hour < 22:
            time_of_day = "evening"
        else:
            time_of_day = "night"

        return ActionOutput(
            action_type=action_input.action_config.type,
            response=DetermineTimeOfDayResponse(
                success=True,
                time_of_day=time_of_day,
            ),
        )