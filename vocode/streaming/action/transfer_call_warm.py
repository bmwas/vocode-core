from typing import Literal, Optional, Type, Union
import time
import asyncio
from loguru import logger
from pydantic.v1 import BaseModel, Field
from vocode.streaming.action.phone_call_action import (
    TwilioPhoneConversationAction,
    VonagePhoneConversationAction,
)
from vocode.streaming.models.actions import ActionConfig as VocodeActionConfig
from vocode.streaming.models.actions import ActionInput, ActionOutput
from vocode.streaming.utils.async_requester import AsyncRequestor
from vocode.streaming.utils.phone_numbers import sanitize_phone_number
from vocode.streaming.utils.state_manager import (
    TwilioPhoneConversationStateManager,
    VonagePhoneConversationStateManager,
)


class WarmTransferCallEmptyParameters(BaseModel):
    pass


class WarmTransferCallRequiredParameters(BaseModel):
    phone_number: str = Field(..., description="The phone number to transfer the call to")


WarmTransferCallParameters = Union[
    WarmTransferCallEmptyParameters, WarmTransferCallRequiredParameters
]


class WarmTransferCallResponse(BaseModel):
    success: bool


class WarmTransferCallVocodeActionConfig(
    VocodeActionConfig, type="action_warm_transfer_call"
):  # type: ignore
    phone_number: Optional[str] = Field(
        None, description="The phone number to transfer the call to"
    )

    def get_phone_number(self, input: ActionInput) -> str:
        if isinstance(input.params, WarmTransferCallRequiredParameters):
            return input.params.phone_number
        elif isinstance(input.params, WarmTransferCallEmptyParameters):
            assert self.phone_number, "phone number must be set"
            return self.phone_number
        else:
            raise TypeError("Invalid input params type")

    def action_attempt_to_string(self, input: ActionInput) -> str:
        phone_number = self.get_phone_number(input)
        return f"Attempting to warm transfer call to {phone_number}"

    def action_result_to_string(self, input: ActionInput, output: ActionOutput) -> str:
        assert isinstance(output.response, WarmTransferCallResponse)
        if output.response.success:
            action_description = "Successfully performed warm transfer of call"
        else:
            action_description = "Did not transfer call because user interrupted"
        return action_description


FUNCTION_DESCRIPTION = """Performs a warm transfer of the call to a manager or supervisor by adding all parties to a conference call."""
QUIET = False
IS_INTERRUPTIBLE = True
SHOULD_RESPOND: Literal["always"] = "always"


class TwilioWarmTransferCall(
    TwilioPhoneConversationAction[
        WarmTransferCallVocodeActionConfig, WarmTransferCallParameters, WarmTransferCallResponse
    ]
):
    description: str = FUNCTION_DESCRIPTION
    response_type: Type[WarmTransferCallResponse] = WarmTransferCallResponse
    conversation_state_manager: TwilioPhoneConversationStateManager

    @property
    def parameters_type(self) -> Type[WarmTransferCallParameters]:
        if self.action_config.phone_number:
            return WarmTransferCallEmptyParameters
        else:
            return WarmTransferCallRequiredParameters

    def __init__(self, action_config: WarmTransferCallVocodeActionConfig):
        super().__init__(
            action_config,
            quiet=QUIET,
            is_interruptible=IS_INTERRUPTIBLE,
            should_respond=SHOULD_RESPOND,
        )

    async def transfer_call(self, twilio_call_sid: str, to_phone: str):
        twilio_client = self.conversation_state_manager.create_twilio_client()
        account_sid = twilio_client.get_telephony_config().account_sid
        auth = twilio_client.auth  # Should be a tuple (username, auth_token)
        async_requestor = AsyncRequestor()
        session = async_requestor.get_session()

        # Create a unique conference name
        conference_name = f'Conference_{twilio_call_sid}_{int(time.time())}'

        # TwiML to join the conference
        twiml_conference = f'<Response><Dial><Conference>{conference_name}</Conference></Dial></Response>'

        # Collect the call SIDs to update
        call_sids_to_update = [twilio_call_sid]

        # Fetch child calls associated with the original call
        calls_list_url = f'https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Calls.json?ParentCallSid={twilio_call_sid}'

        async with session.get(calls_list_url, auth=auth) as response:
            if response.status != 200:
                logger.error(f"Failed to fetch child calls: {response.status} {response.reason}")
                raise Exception("Failed to fetch child calls")
            calls_data = await response.json()
            child_calls = calls_data.get('calls', [])
            for call in child_calls:
                child_call_sid = call.get('sid')
                call_sids_to_update.append(child_call_sid)

        # Update all calls to join the conference
        for call_sid in call_sids_to_update:
            update_call_url = f'https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Calls/{call_sid}.json'
            update_payload = {
                'Twiml': twiml_conference
            }

            async with session.post(update_call_url, data=update_payload, auth=auth) as response:
                if response.status not in [200, 201, 204]:
                    logger.error(f"Failed to update call {call_sid}: {response.status} {response.reason}")
                    raise Exception(f"Failed to update call {call_sid}")
                else:
                    logger.info(f"Call {call_sid} updated to join conference {conference_name}")

        # Determine the 'From' phone number
        if self.conversation_state_manager.get_direction() == "outbound":
            from_phone_number = self.conversation_state_manager.get_from_phone()
        else:
            from_phone_number = self.conversation_state_manager.get_to_phone()

        if not from_phone_number:
            logger.error("Twilio 'From' phone number is not set")
            raise Exception("Twilio 'From' phone number is not set")

        # Add the third party to the conference
        participant_payload = {
            'From': "+17139292951",
            'To': "+18323858954",
            'Twiml': twiml_conference
        }

        add_participant_url = f'https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Calls.json'

        async with session.post(add_participant_url, data=participant_payload, auth=auth) as response:
            if response.status not in [200, 201]:
                logger.error(f"Failed to call participant: {response.status} {response.reason}")
                raise Exception("Failed to call participant")
            else:
                logger.info(f"Called participant {to_phone} to join conference {conference_name}")

                # Optionally return participant SID
                participant_data = await response.json()
                participant_call_sid = participant_data.get('sid')

                return participant_call_sid

    async def run(self, action_input: ActionInput[WarmTransferCallParameters]) -> ActionOutput[WarmTransferCallResponse]:
        twilio_call_sid = self.get_twilio_sid(action_input)
        phone_number = self.action_config.get_phone_number(action_input)
        sanitized_phone_number = sanitize_phone_number(phone_number)

        if action_input.user_message_tracker is not None:
            await action_input.user_message_tracker.wait()

            logger.info("Finished waiting for user message tracker, now attempting to warm transfer call")

            if self.conversation_state_manager.transcript.was_last_message_interrupted():
                logger.info("Last bot message was interrupted, not transferring call")
                return ActionOutput(
                    action_type=action_input.action_config.type,
                    response=WarmTransferCallResponse(success=False),
                )

        await self.transfer_call(twilio_call_sid, sanitized_phone_number)
        
        return ActionOutput(
            action_type=action_input.action_config.type,
            response=WarmTransferCallResponse(success=True),
        )
