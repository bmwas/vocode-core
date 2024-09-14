import asyncio
from typing import Literal, Optional, Type, Union, Tuple

from loguru import logger
from pydantic.v1 import BaseModel, Field

import os

from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import Mail

from vocode.streaming.action.phone_call_action import TwilioPhoneConversationAction
from vocode.streaming.models.actions import ActionConfig as VocodeActionConfig
from vocode.streaming.models.actions import ActionInput, ActionOutput
from vocode.streaming.utils.state_manager import TwilioPhoneConversationStateManager

# Define the parameters
class SendEmailEmptyParameters(BaseModel):
    pass


class SendEmailRequiredParameters(BaseModel):
    to_email: str = Field(..., description="The email address to send an email to")
    subject: str = Field(..., description="The subject of the email")
    email_body: str = Field(..., description="The body of the email")


SendEmailParameters = Union[SendEmailEmptyParameters, SendEmailRequiredParameters]

# Define the response model
class SendEmailResponse(BaseModel):
    success: bool

# Define the action configuration
class SendEmailVocodeActionConfig(
    VocodeActionConfig, type="action_send_email"
):  # type: ignore
    to_email: Optional[str] = Field(
        None, description="The email address to send an email to"
    )
    subject: Optional[str] = Field(
        None, description="Subject of the email to be sent."
    )
    email_body: Optional[str] = Field(
        None, description="Body of the email to be sent."
    )

    def get_email_details(self, input: ActionInput) -> Tuple[str, str, str]:
        if isinstance(input.params, SendEmailRequiredParameters):
            logger.debug("Using email details from input parameters.")
            return (
                input.params.to_email,
                input.params.subject,
                input.params.email_body,
            )
        elif isinstance(input.params, SendEmailEmptyParameters):
            logger.debug("Using email details from action configuration.")
            if not (self.to_email and self.subject and self.email_body):
                logger.error("Email details must be set in the action configuration.")
                raise ValueError("Email details must be set in the action configuration.")
            return self.to_email, self.subject, self.email_body
        else:
            logger.error("Invalid input params type.")
            raise TypeError("Invalid input params type")

    def action_attempt_to_string(self, input: ActionInput) -> str:
        to_email = (
            self.to_email
            if isinstance(input.params, SendEmailEmptyParameters)
            else input.params.to_email
        )
        return f"Attempting to send email to {to_email}"

    def action_result_to_string(
        self, input: ActionInput, output: ActionOutput
    ) -> str:
        if output.response.success:
            return "Email sent successfully."
        else:
            return "Failed to send email."

# Constants
FUNCTION_DESCRIPTION = "Sends an email during an ongoing call."
QUIET = True
IS_INTERRUPTIBLE = True
SHOULD_RESPOND: Literal["always"] = "always"

# Define the action class
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
        if (
            self.action_config.to_email
            and self.action_config.subject
            and self.action_config.email_body
        ):
            logger.debug("No input parameters required; using SendEmailEmptyParameters.")
            return SendEmailEmptyParameters
        else:
            logger.debug("Input parameters required; using SendEmailRequiredParameters.")
            return SendEmailRequiredParameters

    def __init__(
        self,
        action_config: SendEmailVocodeActionConfig,
    ):
        super().__init__(
            action_config,
            quiet=QUIET,
            is_interruptible=IS_INTERRUPTIBLE,  # Use the constant for consistency
            should_respond=SHOULD_RESPOND,
        )

    def send_email(self, to_email: str, subject: str, email_body: str):
        logger.debug("Preparing to send email.")
        sendgrid_api_key = os.environ.get("SENDGRID_API_KEY")
        from_email = os.environ.get("SENDGRID_FROM_EMAIL")
        if not sendgrid_api_key or not from_email:
            logger.error(
                "SENDGRID_API_KEY and SENDGRID_FROM_EMAIL must be set in environment variables."
            )
            return None
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
            return response
        except Exception as e:
            logger.error(f"Exception occurred while sending email: {str(e)}")
            return None

    async def run(
        self, action_input: ActionInput[SendEmailParameters]
    ) -> ActionOutput[SendEmailResponse]:
        logger.debug("Starting the email send action.")
        to_email, subject, email_body = self.action_config.get_email_details(
            action_input
        )
        logger.debug(f"Email details retrieved - To: {to_email}, Subject: {subject}")

        if action_input.user_message_tracker is not None:
            logger.debug("Waiting for user message tracker to finish.")
            await action_input.user_message_tracker.wait()
            logger.debug("User message tracker has finished.")

        try:
            loop = asyncio.get_running_loop()
            response = await loop.run_in_executor(
                None, self.send_email, to_email, subject, email_body
            )
            if response and response.status_code == 202:  # SendGrid returns 202 on success
                success = True
                logger.info("Email sent successfully.")
            else:
                success = False
                logger.error("Failed to send email.")
        except Exception as e:
            logger.error(f"Error sending email: {str(e)}")
            success = False

        logger.debug("Email send action completed.")
        return ActionOutput(
            action_type=action_input.action_config.type,
            response=SendEmailResponse(success=success),
        )