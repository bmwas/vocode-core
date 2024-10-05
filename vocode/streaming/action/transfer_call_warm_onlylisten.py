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
import aiohttp


class ListenOnlyWarmTransferCallEmptyParameters(BaseModel):
    pass


class ListenOnlyWarmTransferCallRequiredParameters(BaseModel):
    phone_number: str = Field(..., description="The phone number to transfer the call to")


ListenOnlyWarmTransferCallParameters = Union[
    ListenOnlyWarmTransferCallEmptyParameters, ListenOnlyWarmTransferCallRequiredParameters
]


class ListenOnlyWarmTransferCallResponse(BaseModel):
    success: bool


class ListenOnlyWarmTransferCallVocodeActionConfig(
    VocodeActionConfig, type="action_listen_only_warm_transfer_call"
):  # type: ignore
    phone_number: Optional[str] = Field(
        None, description="The phone number to transfer the call to"
    )

    def get_phone_number(self, input: ActionInput) -> str:
        if isinstance(input.params, ListenOnlyWarmTransferCallRequiredParameters):
            return input.params.phone_number
        elif isinstance(input.params, ListenOnlyWarmTransferCallEmptyParameters):
            assert self.phone_number, "phone number must be set"
            return self.phone_number
        else:
            raise TypeError("Invalid input params type")

    def action_attempt_to_string(self, input: ActionInput) -> str:
        phone_number = self.get_phone_number(input)
        return f"Attempting to perform a listen-only warm transfer call to {phone_number}"

    def action_result_to_string(self, input: ActionInput, output: ActionOutput) -> str:
        assert isinstance(output.response, ListenOnlyWarmTransferCallResponse)
        if output.response.success:
            action_description = "Successfully performed listen-only warm transfer of call"
        else:
            action_description = "Did not transfer call because user interrupted"
        return action_description


FUNCTION_DESCRIPTION = """Performs a listen-only warm transfer of the call to a manager or supervisor by adding all parties to a conference call where the supervisor can only listen.
MUST keep talking as you await other participants to join the conference call"""
QUIET = False
IS_INTERRUPTIBLE = False
SHOULD_RESPOND: Literal["always"] = "always"


class TwilioListenOnlyWarmTransferCall(
    TwilioPhoneConversationAction[
        ListenOnlyWarmTransferCallVocodeActionConfig,
        ListenOnlyWarmTransferCallParameters,
        ListenOnlyWarmTransferCallResponse,
    ]
):
    description: str = FUNCTION_DESCRIPTION
    response_type: Type[ListenOnlyWarmTransferCallResponse] = ListenOnlyWarmTransferCallResponse
    conversation_state_manager: TwilioPhoneConversationStateManager

    @property
    def parameters_type(self) -> Type[ListenOnlyWarmTransferCallParameters]:
        if self.action_config.phone_number:
            return ListenOnlyWarmTransferCallEmptyParameters
        else:
            return ListenOnlyWarmTransferCallRequiredParameters

    def __init__(self, action_config: ListenOnlyWarmTransferCallVocodeActionConfig):
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

        # TwiML for the first participant (caller) to start the conference
        participant_twiml = f'''
<Response>
    <Dial>
        <Conference startConferenceOnEnter="true" endConferenceOnExit="false" waitUrl="" >{conference_name}</Conference>
    </Dial>
</Response>
'''

        # TwiML for additional participants (callee and supervisor) to join the conference
        additional_participant_twiml = f'''
<Response>
    <Dial>
        <Conference startConferenceOnEnter="false" endConferenceOnExit="false" waitUrl="" >{conference_name}</Conference>
    </Dial>
</Response>
'''

        # Collect the call SIDs to update (caller and callee)
        call_sids_to_update = [twilio_call_sid]

        # Fetch child calls associated with the original call
        calls_list_url = f'https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Calls.json?ParentCallSid={twilio_call_sid}'

        async with session.get(calls_list_url, auth=auth) as response:
            if response.status != 200:
                response_text = await response.text()
                logger.error(f"Failed to fetch child calls: {response.status} {response.reason} - {response_text}")
                raise Exception("Failed to fetch child calls")
            calls_data = await response.json()
            child_calls = calls_data.get('calls', [])
            for call in child_calls:
                child_call_sid = call.get('sid')
                call_sids_to_update.append(child_call_sid)

        # Update caller and callee to join the conference
        for idx, call_sid in enumerate(call_sids_to_update):
            update_call_url = f'https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Calls/{call_sid}.json'
            if idx == 0:
                # First participant starts the conference
                update_payload = {'Twiml': participant_twiml}
            else:
                # Additional participants join without starting the conference
                update_payload = {'Twiml': additional_participant_twiml}

            try:
                async with session.post(update_call_url, data=update_payload, auth=auth) as response:
                    if response.status not in [200, 201, 204]:
                        response_text = await response.text()
                        logger.error(f"Failed to update call {call_sid}: {response.status} {response.reason} - {response_text}")
                        raise Exception(f"Failed to update call {call_sid}")
                    else:
                        logger.info(f"Call {call_sid} updated to join conference {conference_name}")
                await asyncio.sleep(0.5)  # Short delay to prevent API rate limiting
            except Exception as e:
                logger.error(f"Exception while updating call {call_sid}: {e}")
                raise

        # Determine the 'From' phone number for adding the supervisor
        if self.conversation_state_manager.get_direction() == "outbound":
            conf_add_phone_number = self.conversation_state_manager.get_from_phone()
       
