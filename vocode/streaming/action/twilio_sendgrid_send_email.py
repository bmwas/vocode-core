import asyncio
import os
import re
from typing import Optional, Type

from loguru import logger
from pydantic import BaseModel, Field

from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import Mail

from vocode.streaming.action.base_action import BaseAction
from vocode.streaming.models.actions import ActionConfig
from vocode.streaming.models.actions import ActionInput, ActionOutput

class SendEmailVocodeActionConfig(ActionConfig, type="sendgrid_send_email"):  # type: ignore
    pass

class SendEmailParameters(BaseModel):
    to_email: str = Field(..., description="The email address to send an email to.")
    subject: str = Field(..., description="The subject of the email.")
    email_body: str = Field(..., description="The body of the email.")

class SendEmailResponse(BaseModel):
    success: bool
    message: Optional[str] = None

class SendEmail(
    BaseAction[
        SendEmailVocodeActionConfig,
        SendEmailParameters,
        SendEmailResponse,
    ]
):
    description: str = "Sends an email to a specified email address."
    parameters_type: Type[SendEmailParameters] = SendEmailParameters
    response_type: Type[SendEmailResponse] = SendEmailResponse

    def __init__(
        self,
        action_config: SendEmailVocodeActionConfig,
    ):
        super().__init__(
            action_config,
            quiet=False,
            should_respond="never",
        )

    def _validate_email(self, email: str) -> bool:
        # Simple email validation regex
        EMAIL_REGEX = r"^[^@]+@[^@]+\.[^@]+$"
        return bool(re.match(EMAIL_REGEX, email))

    def send_email(self, to_email: str, subject: str, email_body: str) -> bool:
        logger.debug("Preparing to send email.")
        sendgrid_api_key = os.environ.get("SENDGRID_API_KEY")
        from_email = os.environ.get("SENDGRID_FROM_EMAIL")
        if not sendgrid_api_key or not from_email:
            logger.error(
                "SENDGRID_API_KEY and SENDGRID_FROM_EMAIL must be set in environment variables."
            )
            return False
        if not self._validate_email(to_email):
            logger.error("Invalid recipient email address.")
            return False
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
            return response.status_code == 202  # SendGrid returns 202 on success
        except Exception as e:
            logger.error(f"Exception occurred while sending email: {str(e)}")
            return False

    async def run(
        self, action_input: ActionInput[SendEmailParameters]
    ) -> ActionOutput[SendEmailResponse]:
        logger.debug("Starting the email send action.")

        to_email = action_input.params.to_email
        subject = action_input.params.subject
        email_body = action_input.params.email_body

        logger.debug(f"Email details - To: {to_email}, Subject: {subject}")

        success = await asyncio.get_event_loop().run_in_executor(
            None, self.send_email, to_email, subject, email_body
        )

        if success:
            logger.info("Email sent successfully.")
        else:
            logger.error("Failed to send email.")

        logger.debug("Email send action completed.")
        return ActionOutput(
            action_type=action_input.action_config.type,
            response=SendEmailResponse(success=success),
        )