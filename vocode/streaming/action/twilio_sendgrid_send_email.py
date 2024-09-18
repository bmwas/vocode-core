import os
from typing import Optional, Type

from pydantic import Field
from pydantic.v1 import BaseModel

from sendgrid import SendGridAPIClient  # SendGrid imports
from sendgrid.helpers.mail import Mail

from loguru import logger  # For logging

from vocode.streaming.action.base_action import BaseAction
from vocode.streaming.models.actions import ActionConfig as VocodeActionConfig
from vocode.streaming.models.actions import ActionInput, ActionOutput

_SENDGRID_ACTION_DESCRIPTION = """Sends an email using SendGrid API.
The input to this action is the recipient email, email body, and optional subject.
"""

class SendEmailParameters(BaseModel):
    to_email: str = Field(..., description="The email address to send the email to.")
    subject: Optional[str] = Field(None, description="The subject of the email.")
    email_body: str = Field(..., description="The body of the email.")

class SendEmailResponse(BaseModel):
    success: bool

class SendEmailVocodeActionConfig(
    VocodeActionConfig, type="action_send_email"  # type: ignore
):
    pass

class TwilioSendEmail(
        BaseAction[
            SendEmailVocodeActionConfig,
            SendEmailParameters,
            SendEmailResponse,
        ]
    ):
    description: str = _SENDGRID_ACTION_DESCRIPTION
    parameters_type: Type[SendEmailParameters] = SendEmailParameters
    response_type: Type[SendEmailResponse] = SendEmailResponse

    def __init__(
        self,
        action_config: SendEmailVocodeActionConfig,
    ):
        super().__init__(
            action_config,
            quiet=False,  # Set to False to ensure the action sends a response
            is_interruptible=True,
        )

    async def _end_of_run_hook(self) -> None:
        """This method is called at the end of the run method. It is optional but intended to be
        overridden if needed."""
        print("Successfully sent email!")

    async def run(
        self, action_input: ActionInput[SendEmailParameters]
    ) -> ActionOutput[SendEmailResponse]:
        # Extract parameters
        to_email = action_input.params.to_email.strip()
        subject = (
            action_input.params.subject.strip() if action_input.params.subject else "Email from Vocode"
        )
        email_body = action_input.params.email_body

        # Prepare SendGrid client
        sendgrid_api_key = os.environ.get("SENDGRID_API_KEY")
        from_email = os.environ.get("SENDGRID_FROM_EMAIL")
        if not sendgrid_api_key or not from_email:
            logger.error(
                "SENDGRID_API_KEY and SENDGRID_FROM_EMAIL must be set in environment variables."
            )
            return ActionOutput(
                action_type=action_input.action_config.type,
                response=SendEmailResponse(success=False),
            )

        message = Mail(
            from_email=from_email,
            to_emails=to_email,
            subject=subject,
            html_content=email_body,
        )
        try:
            sg = SendGridAPIClient(sendgrid_api_key)
            response = sg.send(message)
            logger.info(f"Email sent with status code: {response.status_code}")
            success = response.status_code == 202  # SendGrid returns 202 on success
        except Exception as e:
            logger.error(f"Exception occurred while sending email: {str(e)}")
            success = False

        if success:
            await self._end_of_run_hook()
        else:
            logger.error("Failed to send email")

        return ActionOutput(
            action_type=action_input.action_config.type,
            response=SendEmailResponse(success=success),
        )