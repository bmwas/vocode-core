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
    descriptor: str = Field("A human-readable descriptor; e.g., 'The caller or user's name and email.'")
    raw_value: str = Field(
        ...,
        description="The raw value parsed from the transcript.",
    )
    formatted_name: str = Field(
        ...,
        description="The estimated formatted value of the caller's name.",
    )
    formatted_email: str = Field(
        ...,
        description="The estimated formatted value of the caller's email address.",
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
    description: str = """ Attempts to extract and record the caller's full name and email address from the transcript.
    **Requirements:**
    - **Formatted Name (formatted_name):**
    - Should be the caller's full name in proper case.
    - Must exclude any extra words or phrases not part of the name.
    - Examples of correct formatting:
    - "John Doe"
    - "Alice O'Hara"
    - "Dr. Emily Clark" (if titles are appropriate in your context)
    - **Formatted Email (`formatted_email`):**
    - Should be a valid email address formatted according to standard email conventions.
    - Must convert spoken representations to standard email format.
    - Examples of conversions:
    - "john dot doe at example dot com" → "john.doe@example.com"
    - "alice underscore smith at mail dot co dot uk" → "alice_smith@mail.co.uk"
    **Instructions:**
    1. **Parse the Raw Value:**
    - Extract both the name and email address from the `raw_value` provided in the transcript.
    - Handle variations in how people might state their name and email.
    2. **Format the Values:**
    - **Name:**
     - Capitalize appropriately.
     - Remove any fillers or non-name words.
    - **Email:**
     - Replace words like "at" with "@" and "dot" with ".".
     - Remove any spaces and ensure all characters are in the correct places.
    3. **Validation:**
    - **Name Validation:**
     - Check that the name contains only valid characters (letters, hyphens, apostrophes, spaces).
     - Ensure the name is not empty and appears to be a real name.
    - **Email Validation:**
     - Use standard email regex patterns to validate the email structure.
     - Ensure there are no invalid characters or formatting issues.
    4. **Error Handling:**
    - If validation fails for either field, set the corresponding success flag to `False`.
    - Provide meaningful messages to indicate why the validation failed.
    **Examples:**
    - **Example 1:**
    - *Transcript:* "My name is John Doe and my email is J as in John, O as in Orange, H as in Honey and N as in New at example dot com that is - john dot doe at example dot com."
    - *Formatted Name:* "John Doe"
    - *Formatted Email:* "john.doe@example.com"
    - **Example 2:**
    - *Transcript:* "You can reach me at jane-smith at mail dot net. I'm Jane Smith."
    - *Formatted Name:* "Jane Smith"
    - *Formatted Email:* "jane-smith@mail.net"  
    - **Example 3:**
    - *Transcript:* "This is Dr. Emily Clark, email me at e-c-l-a-r-k at university dot edu"
    - *Formatted Name:* "Dr. Emily Clark"
    - *Formatted Email:* "eclark@university.edu"
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