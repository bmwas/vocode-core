import os
from typing import Type

from pydantic.v1 import BaseModel, Field

from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import Mail

from loguru import logger

from vocode.streaming.action.base_action import BaseAction
from vocode.streaming.models.actions import ActionConfig as VocodeActionConfig
from vocode.streaming.models.actions import ActionInput, ActionOutput

_SENDGRID_ACTION_DESCRIPTION = """Sends an email using SendGrid API.
The input to this action is the recipient email, email body, and subject.
"""

class SendEmailParameters(BaseModel):
    to_email: str = Field(..., description="The email address to send the email to.")
    subject: str = Field(..., description="The subject of the email.")
    email_body: str = Field(..., description="The body of the email.")

class SendEmailResponse(BaseModel):
    success: bool

class SendEmailVocodeActionConfig(
    VocodeActionConfig, type="action_send_email"  # type: ignore
):
    pass

class SendEmailAction(
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
            is_interruptible=False,
            should_respond="always",
        )

    async def run(
        self, action_input: ActionInput[SendEmailParameters]
    ) -> ActionOutput[SendEmailResponse]:
        logger.info("Attempting to send email")

        # Use parameters directly from the input
        to_email = action_input.params.to_email.strip()
        subject = action_input.params.subject.strip()
        email_body = action_input.params.email_body

        logger.debug(f"Received parameters - to_email: {to_email}, subject: {subject}, email_body length: {len(email_body)}")

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
            logger.info("Successfully sent email")
        else:
            logger.error("Failed to send email")

        return ActionOutput(
            action_type=action_input.action_config.type,
            response=SendEmailResponse(success=success),
        )

    def action_attempt_to_string(self, input: ActionInput[SendEmailParameters]) -> str:
        return f"Attempting to send email to {input.params.to_email}"

    def action_result_to_string(self, input: ActionInput[SendEmailParameters], output: ActionOutput[SendEmailResponse]) -> str:
        if output.response.success:
            return "Successfully sent email"
        else:
            return "Failed to send email"