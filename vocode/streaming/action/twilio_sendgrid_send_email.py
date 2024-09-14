from typing import Literal, Optional, Type, Union
import os
from loguru import logger
from pydantic.v1 import BaseModel, Field

from vocode.streaming.action.phone_call_action import (
    TwilioPhoneConversationAction,
)
from vocode.streaming.models.actions import ActionConfig as VocodeActionConfig
from vocode.streaming.models.actions import ActionInput, ActionOutput
from vocode.streaming.utils.state_manager import (
    TwilioPhoneConversationStateManager,
)

from twilio.rest import Client


class ContactCenterEmptyParameters(BaseModel):
    pass


ContactCenterParameters = ContactCenterEmptyParameters


class ContactCenterResponse(BaseModel):
    success: bool
    phone_number: str


class ContactCenterVocodeActionConfig(
    VocodeActionConfig, type="action_retrieve_phone_number"
):  # type: ignore
    def action_attempt_to_string(self, input: ActionInput) -> str:
        return "Attempting to retrieve caller's phone number"

    def action_result_to_string(
        self, input: ActionInput, output: ActionOutput
    ) -> str:
        assert isinstance(output.response, ContactCenterResponse)
        if output.response.phone_number != "EMPTY":
            action_description = (
                f"Successfully retrieved phone number: {output.response.phone_number}"
            )
        else:
            action_description = "Could not retrieve phone number"
        return action_description


FUNCTION_DESCRIPTION = "Retrieves the phone number of the caller using Twilio's API."
QUIET = True
IS_INTERRUPTIBLE = True
SHOULD_RESPOND: Literal["always"] = "always"


class TwilioContactCenter(
    TwilioPhoneConversationAction[
        ContactCenterVocodeActionConfig,
        ContactCenterParameters,
        ContactCenterResponse,
    ]
):
    description: str = FUNCTION_DESCRIPTION
    response_type: Type[ContactCenterResponse] = ContactCenterResponse
    conversation_state_manager: TwilioPhoneConversationStateManager

    @property
    def parameters_type(self) -> Type[ContactCenterParameters]:
        return ContactCenterEmptyParameters

    def __init__(
        self,
        action_config: ContactCenterVocodeActionConfig,
    ):
        super().__init__(
            action_config,
            quiet=QUIET,
            is_interruptible=IS_INTERRUPTIBLE,
            should_respond=SHOULD_RESPOND,
        )

    async def get_call_phone_number(self, twilio_call_sid: str):
        logger.debug(f"Fetching phone number for Call SID: {twilio_call_sid}")
        # Initialize the Twilio client
        account_sid = os.environ.get("TWILIO_ACCOUNT_SID")
        auth_token = os.environ.get("TWILIO_AUTH_TOKEN")

        if not account_sid or not auth_token:
            logger.error(
                "TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN environment variables must be set"
            )
            return None

        client = Client(account_sid, auth_token)
        try:
            # Fetch the call details using the Call SID
            call = client.calls(twilio_call_sid).fetch()
            logger.debug(f"Call details retrieved: {call}")
            # Return the 'from' number (caller's number)
            return call.from_
        except Exception as e:
            logger.error(f"Error retrieving phone number: {str(e)}")
            return None

    async def run(
        self, action_input: ActionInput[ContactCenterParameters]
    ) -> ActionOutput[ContactCenterResponse]:
        twilio_call_sid = self.get_twilio_sid(action_input)
        logger.debug(f"Retrieved Twilio Call SID: {twilio_call_sid}")

        if action_input.user_message_tracker is not None:
            await action_input.user_message_tracker.wait()

        logger.info(
            "Finished waiting for user message tracker, now attempting to retrieve caller's phone number"
        )

        phone_number = await self.get_call_phone_number(twilio_call_sid)
        logger.debug(f"Retrieved phone number: {phone_number}")

        if not phone_number:
            phone_number = "EMPTY"

        return ActionOutput(
            action_type=action_input.action_config.type,
            response=ContactCenterResponse(success=True, phone_number=phone_number),
        )