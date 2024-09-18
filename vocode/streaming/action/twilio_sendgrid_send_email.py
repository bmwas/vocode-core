"""
Sends an email to a caller using the Twilio Sendgrid API. 
Must have recipient's Email Address, Subject and Email body.
"""
import os
from typing import Literal, Type

from loguru import logger
from pydantic.v1 import BaseModel, Field

from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import Mail

from vocode.streaming.action.phone_call_action import TwilioPhoneConversationAction
from vocode.streaming.models.actions import ActionConfig as VocodeActionConfig
from vocode.streaming.models.actions import ActionInput, ActionOutput
from vocode.streaming.utils.state_manager import TwilioPhoneConversationStateManager


class SendEmailRequiredParameters(BaseModel):
    to_email: str = Field(..., description="The email address to send the email to")
    subject: str = Field(..., description="The subject of the email")
    email_body: str = Field(..., description="The body of the email")


class SendEmailResponse(BaseModel):
    success: bool
    message: str


class SendEmailVocodeActionConfig(VocodeActionConfig, type="action_send_email"):  # type: ignore
    pass  # No longer need to store parameters here


FUNCTION_DESCRIPTION = """ 
Sends an email during an ongoing call using SendGrid API.
The input to this action is the recipient's email address, email body, and subject.
The email address, email subject, and email body are all required parameters.
"""
QUIET = False
IS_INTERRUPTIBLE = True
SHOULD_RESPOND: Literal["always"] = "always"


class TwilioSendEmail(
    TwilioPhoneConversationAction[
        SendEmailVocodeActionConfig, SendEmailRequiredParameters, SendEmailResponse
    ]
):
    description: str = FUNCTION_DESCRIPTION
    response_type: Type[SendEmailResponse] = SendEmailResponse
    conversation_state_manager: TwilioPhoneConversationStateManager

    @property
    def parameters_type(self) -> Type[SendEmailRequiredParameters]:
        # Always expect required parameters
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

    async def send_email(self, to_email: str, subject: str, email_body: str) -> Tuple[bool, str]:
        logger.debug("Preparing to send email.")
        sendgrid_api_key = os.environ.get("SENDGRID_API_KEY")
        from_email = os.environ.get("SENDGRID_FROM_EMAIL")
        if not sendgrid_api_key or not from_email:
            error_message = (
                "SENDGRID_API_KEY and SENDGRID_FROM_EMAIL must be set in environment variables."
            )
            logger.error(error_message)
            return False, error_message

        message = Mail(
            from_email=from_email,
            to_emails=to_email,
            subject=subject,
            html_content=email_body,
        )
        try:
            sg = SendGridAPIClient(sendgrid_api_key)
            response = sg.send(message)
            if response.status_code == 202:
                success_message = f"Email sent successfully to {to_email}."
                logger.info(success_message)
                return True, success_message
            else:
                error_message = f"Failed to send email. Status code: {response.status_code}"
                logger.error(error_message)
                return False, error_message
        except Exception as e:
            error_message = f"Exception occurred while sending email: {str(e)}"
            logger.error(error_message)
            return False, error_message

    async def run(
        self, action_input: ActionInput[SendEmailRequiredParameters]
    ) -> ActionOutput[SendEmailResponse]:
        if action_input.user_message_tracker is not None:
            await action_input.user_message_tracker.wait()

        logger.info(
            "Finished waiting for user message tracker, now attempting to send email"
        )

        if self.conversation_state_manager.transcript.was_last_message_interrupted():
            logger.info("Last bot message was interrupted, not sending email")
            return ActionOutput(
                action_type=action_input.action_config.type,
                response=SendEmailResponse(
                    success=False,
                    message="Email sending was aborted due to interruption."
                ),
            )

        # Directly use parameters from action_input.params
        to_email = action_input.params.to_email
        subject = action_input.params.subject
        email_body = action_input.params.email_body

        success, message = await self.send_email(to_email, subject, email_body)

        return ActionOutput(
            action_type=action_input.action_config.type,
            response=SendEmailResponse(success=success, message=message),
        )