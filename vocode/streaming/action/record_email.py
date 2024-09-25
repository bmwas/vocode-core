import re
from typing import Optional, Type

from pydantic.v1 import BaseModel, Field

from vocode.streaming.action.base_action import BaseAction
from vocode.streaming.models.actions import ActionConfig, ActionInput, ActionOutput

EMAIL_REGEX = r"^(?!\.)(?!.*\.\.)[a-zA-Z0-9._%+-]+(?<!\.)@(?![.])[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$"
NAME_REGEX = r"^[a-zA-Z ,.'-]+$"


class RecordEmailVocodeActionConfig(ActionConfig, type="action_record_email"):  # type: ignore
    pass


class RecordEmailParameters(BaseModel):
    descriptor: str = Field("A human-readable descriptor; e.g., 'The user's name and email.'")
    raw_value: str = Field(
        ...,
        description="The raw value parsed from the transcript.",
    )
    formatted_name: str = Field(
        ...,
        description="The estimated formatted value of the name.",
    )
    formatted_email: str = Field(
        ...,
        description="The estimated formatted value of the email.",
    )


class RecordEmailResponse(BaseModel):
    success: bool
    name_success: bool
    email_success: bool
    name: Optional[str] = None
    email: Optional[str] = None
    message: Optional[str] = None


class RecordEmail(
    BaseAction[
        RecordEmailVocodeActionConfig,
        RecordEmailParameters,
        RecordEmailResponse,
    ]
):
    description: str = """Attempts to record a name and email from the transcript.

    You must format the values to match the field types, e.g.:

    'My name is John Doe and my email is john dot doe at example dot com' ->
    formatted_name: 'John Doe'
    formatted_email: 'john.doe@example.com'

    This function will perform extra validation.
    """
    parameters_type: Type[RecordEmailParameters] = RecordEmailParameters
    response_type: Type[RecordEmailResponse] = RecordEmailResponse

    def __init__(
        self,
        action_config: RecordEmailVocodeActionConfig,
    ):
        super().__init__(
            action_config,
            quiet=False,
            should_respond="never",
        )

    def _validate_email(self, email: str) -> bool:
        return bool(re.match(EMAIL_REGEX, email))

    def _validate_name(self, name: str) -> bool:
        return bool(re.match(NAME_REGEX, name))

    async def run(
        self, action_input: ActionInput[RecordEmailParameters]
    ) -> ActionOutput[RecordEmailResponse]:
        name = action_input.params.formatted_name
        email = action_input.params.formatted_email

        name_success = self._validate_name(name)
        email_success = self._validate_email(email)

        success = name_success and email_success

        return ActionOutput(
            action_type=action_input.action_config.type,
            response=RecordEmailResponse(
                success=success,
                name_success=name_success,
                email_success=email_success,
                name=name if name_success else None,
                email=email if email_success else None,
            ),
        )