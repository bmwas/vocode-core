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
    VocodeActionConfig, type="action_warm_transfer_call"
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
        return f"Attempting to warm transfer call to {phone_number}"

    def action_result_to_string(self, input: ActionInput, output: ActionOutput) -> str:
        assert isinstance(output.response, ListenOnlyWarmTransferCallResponse)
        if output.response.success:
            action_description = "Successfully performed warm transfer of call"
        else:
            action_description = "Did not transfer call because user interrupted"
        return action_description


FUNCTION_DESCRIPTION = """Performs a warm transfer of the call to a manager or supervisor by adding all parties to a conference call.
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

        # TwiML to join the conference (for agent and provider)
        twiml_conference = f'''
        <Response>
            <Dial>
                <Conference startConferenceOnEnter="true" endConferenceOnExit="false" waitUrl="">{conference_name}</Conference>
            </Dial>
        </Response>
        '''

        # Fetch child calls and update to join conference
        call_sids_to_update = [twilio_call_sid]
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

        # Determine the phone number to use for adding supervisor
        if self.conversation_state_manager.get_direction() == "outbound":
            conf_add_phone_number = self.conversation_state_manager.get_to_phone() 
        else:
            conf_add_phone_number = self.conversation_state_manager.get_from_phone()

        if not conf_add_phone_number:
            logger.error("Twilio 'From' phone number is not set")
            raise Exception("Twilio 'From' phone number is not set")

        # **New TwiML to add the supervisor as a coach**
        # Here, we set the coach parameter to the agent's Call SID (twilio_call_sid)
        # This allows the supervisor to monitor and whisper to the agent
        twiml_conference_coach = f'''
        <Response>
            <Dial>
                <Conference coach="{twilio_call_sid}" startConferenceOnEnter="true" endConferenceOnExit="true" waitUrl="">{conference_name}</Conference>
            </Dial>
        </Response>
        '''

        # Add the supervisor to the conference as a coach
        participant_payload = {
            'From': conf_add_phone_number,
            'To': to_phone,
            'Twiml': twiml_conference_coach
        }

        add_participant_url = f'https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Calls.json'

        async with session.post(add_participant_url, data=participant_payload, auth=auth) as response:
            if response.status not in [200, 201]:
                logger.error(f"Failed to call participant: {response.status} {response.reason}")
                raise Exception("Failed to call participant")
            else:
                logger.info(f"Called participant {to_phone} to join conference {conference_name} as coach")

                # Optionally return participant SID
                participant_data = await response.json()
                participant_call_sid = participant_data.get('sid')

                return participant_call_sid

    async def run(self, action_input: ActionInput[ListenOnlyWarmTransferCallParameters]) -> ActionOutput[ListenOnlyWarmTransferCallResponse]:
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
                    response=ListenOnlyWarmTransferCallResponse(success=False),
                )

        # Call the modified transfer_call method
        await self.transfer_call(twilio_call_sid, sanitized_phone_number)
        
        return ActionOutput(
            action_type=action_input.action_config.type,
            response=ListenOnlyWarmTransferCallResponse(success=True),
        )
