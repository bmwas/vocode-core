import os
from typing import Literal, Optional, Type
from loguru import logger
from pydantic.v1 import BaseModel, Field
import sendgrid
from sendgrid.helpers.mail import Mail

from vocode.streaming.action.phone_call_action import (
    TwilioPhoneConversationAction,
    VonagePhoneConversationAction,
)
from vocode.streaming.models.actions import ActionConfig as VocodeActionConfig
from vocode.streaming.models.actions import ActionInput, ActionOutput
from vocode.streaming.utils.async_requester import AsyncRequestor
from vocode.streaming.utils.state_manager import (
    TwilioPhoneConversationStateManager,
    VonagePhoneConversationStateManager,
)

# Define the required parameters for sending an email
class SendEmailRequiredParameters(BaseModel):
    to_email: str = Field(..., description="The recipient's email address")
    subject: str = Field(..., description="The subject of the email")
    email_body: str = Field(..., description="The body content of the email")

SendEmailParameters = SendEmailRequiredParameters


class SendEmailResponse(BaseModel):
    success: bool


class SendEmailVocodeActionConfig(VocodeActionConfig, type="action_send_email"):  # type: ignore
    def action_attempt_to_string(self, input: ActionInput) -> str:
        return f"Attempting to send an email to {input.params.to_email}"

    def action_result_to_string(self, input: ActionInput, output: ActionOutput) -> str:
        assert isinstance(output.response, SendEmailResponse)
        if output.response.success:
            action_description = "Successfully sent the email"
        else:
            action_description = "Failed to send the email"
        return action_description


FUNCTION_DESCRIPTION = "Sends an email to the specified recipient during the call."
QUIET = True
IS_INTERRUPTIBLE = True
SHOULD_RESPOND: Literal["always"] = "always"


class TwilioSendEmail(
    TwilioPhoneConversationAction[
        SendEmailVocodeActionConfig, SendEmailParameters, SendEmailResponse
    ]
):
    description: str = FUNCTION_DESCRIPTION
    response_type: Type[SendEmailResponse] = SendEmailResponse
    conversation_state_manager: TwilioPhoneConversationStateManager

    @property
    def parameters_type(self) -> Type[SendEmailParameters]:
        return SendEmailRequiredParameters

    def __init__(
        self,
        action_config: SendEmailVocodeActionConfig,
    ):
        super().__init__(
            action_config,
            quiet=QUIET,
            is_interruptible=False,
            should_respond=SHOULD_RESPOND,
        )

    async def send_email(self, to_email: str, subject: str, email_body: str):
        logger.debug("Preparing to send email.")
        sendgrid_api_key = os.environ.get("SENDGRID_API_KEY")
        from_email = os.environ.get("SENDGRID_FROM_EMAIL")

        sg = sendgrid.SendGridAPIClient(sendgrid_api_key)
        message = Mail(
            from_email=from_email,  # Using environment variable for sender's email address
            to_emails=to_email,
            subject=subject,
            html_content=email_body
        )
        try:
            response = sg.send(message)
            if response.status_code == 202:
                logger.info("Email sent successfully.")
                return True
            else:
                logger.error(f"Failed to send email: {response.status_code}")
                return False
        except Exception as e:
            logger.error(f"An error occurred while sending email: {str(e)}")
            return False

    async def run(
        self, action_input: ActionInput[SendEmailParameters]
    ) -> ActionOutput[SendEmailResponse]:
        if action_input.user_message_tracker is not None:
            await action_input.user_message_tracker.wait()

        logger.info("Finished waiting for user message tracker, now attempting to send an email")

        email_params = action_input.params
        success = await self.send_email(email_params.to_email, email_params.subject, email_params.email_body)

        return ActionOutput(
            action_type=action_input.action_config.type,
            response=SendEmailResponse(success=success),
        )