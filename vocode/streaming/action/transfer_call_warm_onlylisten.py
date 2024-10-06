from typing import Literal, Optional, Type, Union
import asyncio
from loguru import logger
from pydantic.v1 import BaseModel, Field
from twilio.rest import Client
from twilio.twiml.voice_response import VoiceResponse, Dial, Conference
from vocode.streaming.action.phone_call_action import TwilioPhoneConversationAction
from vocode.streaming.models.actions import ActionConfig as VocodeActionConfig
from vocode.streaming.models.actions import ActionInput, ActionOutput
from vocode.streaming.utils.phone_numbers import sanitize_phone_number


# Define Parameters and Response Models
class ListenOnlyWarmTransferCallEmptyParameters(BaseModel):
    pass


class ListenOnlyWarmTransferCallRequiredParameters(BaseModel):
    phone_number: str = Field(..., description="The supervisor's phone number to transfer the call to")


ListenOnlyWarmTransferCallParameters = Union[
    ListenOnlyWarmTransferCallEmptyParameters, ListenOnlyWarmTransferCallRequiredParameters
]


class ListenOnlyWarmTransferCallResponse(BaseModel):
    success: bool


# Action Configuration
class ListenOnlyWarmTransferCallVocodeActionConfig(
    VocodeActionConfig, type="action_listen_only_warm_transfer_call"
):  # type: ignore
    phone_number: Optional[str] = Field(
        None, description="The supervisor's phone number to transfer the call to"
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


# Action Class
class TwilioListenOnlyWarmTransferCall(
    TwilioPhoneConversationAction[
        ListenOnlyWarmTransferCallVocodeActionConfig,
        ListenOnlyWarmTransferCallParameters,
        ListenOnlyWarmTransferCallResponse,
    ]
):
    description: str = "Performs a listen-only warm transfer of the call to a supervisor by adding all parties to a conference call where the supervisor can only listen."
    response_type: Type[ListenOnlyWarmTransferCallResponse] = ListenOnlyWarmTransferCallResponse

    def __init__(self, action_config: ListenOnlyWarmTransferCallVocodeActionConfig):
        super().__init__(
            action_config,
            quiet=False,
            is_interruptible=False,
            should_respond="always",
        )

    @property
    def parameters_type(self) -> Type[ListenOnlyWarmTransferCallParameters]:
        if self.action_config.phone_number:
            return ListenOnlyWarmTransferCallEmptyParameters
        else:
            return ListenOnlyWarmTransferCallRequiredParameters

async def transfer_call(self, twilio_call_sid: str, supervisor_phone: str):
    # Initialize Twilio Client
    twilio_client = self.conversation_state_manager.create_twilio_client()
    telephony_config = twilio_client.get_telephony_config()
    account_sid = telephony_config.account_sid
    auth_token = telephony_config.auth_token
    client = Client(account_sid, auth_token)

    # Define Conference Name
    conference_name = f'Conference_{twilio_call_sid}'

    # Step 1: Update Existing Call to Join Conference
    logger.info(f"Moving existing call {twilio_call_sid} to conference {conference_name}")
    try:
        twiml = VoiceResponse()
        dial = Dial()
        dial.conference(
            conference_name,
            start_conference_on_enter=True,
            end_conference_on_exit=False,
            beep=False
        )
        twiml.append(dial)
        client.calls(twilio_call_sid).update(twiml=str(twiml))
        logger.info(f"Call {twilio_call_sid} updated to join conference {conference_name}")
    except Exception as e:
        logger.error(f"Error moving call to conference: {e}")
        raise

    # Step 2: Add Supervisor to Conference (Muted)
    supervisor_number = sanitize_phone_number(supervisor_phone)
    logger.info(f"Adding supervisor {supervisor_number} to conference {conference_name} as muted")
    try:
        twiml_supervisor = VoiceResponse()
        dial_supervisor = Dial()
        dial_supervisor.conference(
            conference_name,
            start_conference_on_enter=True,
            end_conference_on_exit=False,
            beep=False,
            muted=True  # Mute the supervisor upon joining
        )
        twiml_supervisor.append(dial_supervisor)
        supervisor_call = client.calls.create(
            to=supervisor_number,
            from_=self.conversation_state_manager.get_from_phone(),  # Twilio number
            twiml=str(twiml_supervisor)
        )
        logger.info(f"Supervisor call initiated with SID {supervisor_call.sid}")
    except Exception as e:
        logger.error(f"Error adding supervisor to conference: {e}")
        raise

    # Step 3: Wait for Conference to be Active
    conference_sid = None
    max_attempts = 15
    attempt = 0
    while attempt < max_attempts:
        try:
            conferences = client.conferences.list(friendly_name=conference_name, limit=1)
            if conferences:
                conference_sid = conferences[0].sid
                logger.info(f"Conference SID retrieved: {conference_sid}")
                break
        except Exception as e:
            logger.error(f"Error retrieving conference details: {e}")
        await asyncio.sleep(1)
        attempt += 1
        logger.info(f"Waiting for conference {conference_name} to be active. Attempt {attempt}/{max_attempts}")

    if not conference_sid:
        logger.error(f"Conference {conference_name} not found after {max_attempts} attempts")
        raise Exception(f"Conference {conference_name} not found after {max_attempts} attempts")

    logger.info(f"Listen-only warm transfer to supervisor {supervisor_number} completed successfully")
