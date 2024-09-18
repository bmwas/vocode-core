import os
from typing import Literal, Optional, Type, Union

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


SendEmailParameters = SendEmailRequiredParameters


class SendEmailResponse(BaseModel):
    success: bool
    message: Optional[str] = None  # Added message field for response


class SendEmailVocodeActionConfig(VocodeActionConfig, type="action_send_email"):  # type: ignore
    to_email: Optional[str] = Field(
        None, description="The email address to send the email to"
    )
    subject: Optional[str] = Field(
        None, description="The subject of the email"
    )
    email_body: Optional[str] = Field(
        None, description="The body of the email"
    )

    def get_email_details(self, input: ActionInput) -> tuple:
        if self.to_email and self.subject and self.email_body:
            # Use parameters from configuration
            logger.debug("Using email details from configuration.")
            return self.to_email, self.subject, self.email_body
        else:
            # Use parameters from input.params
            logger.debug("Using email details from action input parameters.")
            if isinstance(input.params, SendEmailRequiredParameters):
                return (
                    input.params.to_email,
                    input.params.subject,
                    input.params.email_body,
                )
            else:
                raise ValueError("Email details must be provided either in configuration or action parameters.")

    def action_attempt_to_string(self, input: ActionInput) -> str:
        to_email, _, _ = self.get_email_details(input)
        return f"Attempting to send email to {to_email}"

    def action_result_to_string(self, input: ActionInput, output: ActionOutput) -> str:
        if output.response.success:
            return "Successfully sent email"
        else:
            return "Failed to send email"


FUNCTION_DESCRIPTION = """
Sends an email during an ongoing call using SendGrid API.
If the recipient's email address, email subject, and email body are provided in the configuration, they will be used.
Otherwise, the email address, email subject, and email body must be provided as parameters during the action call.
"""

QUIET = False
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
    def parameters_type(self) -> Optional[Type[SendEmailParameters]]:
        if (
            self.action_config.to_email
            and self.action_config.subject
            and self.action_config.email_body
        ):
            # No parameters needed during action call
            return None  # Indicating no parameters are required
        else:
            # Parameters are required during action call
            return SendEmailRequiredParameters

    def include_parameters_in_prompt(self) -> bool:
        # If parameters are provided in configuration, do not include them in the LLM prompt
        return not (
            self.action_config.to_email
            and self.action_config.subject
            and self.action_config.email_body
        )

    def __init__(
        self,
        action_config: SendEmailVocodeActionConfig,
    ):
        super().__init__(
            action_config,
            quiet=QUIET,
            is_interruptible=IS_INTERRUPTIBLE,
            should_respond=SHOULD_RESPOND,
        )

    async def send_email(self, to_email: str, subject: str, email_body: str) -> tuple:
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
        self, action_input: ActionInput[SendEmailParameters]
    ) -> ActionOutput[SendEmailResponse]:
        if action_input.user_message_tracker is not None:
            await action_input.user_message_tracker.wait()

        logger.info(
            "Finished waiting for user message tracker, now attempting to send email"
        )

        # Fetch email details using the updated method
        to_email, subject, email_body = self.action_config.get_email_details(action_input)
        subject= "Hello ..."
        email_body = "Hello, testing this now!!!"
        success, message = await self.send_email(to_email, subject, email_body)

        return ActionOutput(
            action_type=action_input.action_config.type,
            response=SendEmailResponse(success=success, message=message),
        )