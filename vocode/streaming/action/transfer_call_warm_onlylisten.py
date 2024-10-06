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
            quiet=False,  # Enable logging
            is_interruptible=False,
            should_respond="always",  # Ensure the action responds
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

        # Define Conference Name (Consistent Naming)
        conference_name = f'Conference_{twilio_call_sid}'

        # Step 1: Move Agent's Call to Conference via TwiML
        logger.info(f"Moving agent's call {twilio_call_sid} to conference {conference_name}")
        try:
            twiml_agent = VoiceResponse()
            dial_agent = Dial()
            dial_agent.conference(
                conference_name,
                start_conference_on_enter=True,  # Agent starts the conference
                end_conference_on_exit=False,     # Conference remains active
                beep=False
            )
            twiml_agent.append(dial_agent)
            client.calls(twilio_call_sid).update(twiml=str(twiml_agent))
            logger.info(f"Agent's call updated to join conference {conference_name}")
        except Exception as e:
            logger.error(f"Error moving agent's call to conference: {e}")
            raise

        # Short delay to allow TwiML to process
        await asyncio.sleep(2)

        # Step 2: Add Provider to Conference via New Call
        direction = self.conversation_state_manager.get_direction()
        if direction == "outbound":
            provider_phone = self.conversation_state_manager.get_to_phone()
        else:
            provider_phone = self.conversation_state_manager.get_from_phone()

        if not provider_phone:
            logger.error("Provider phone number is not set")
            raise Exception("Provider phone number is not set")

        sanitized_provider_number = sanitize_phone_number(provider_phone)
        logger.info(f"Adding provider {sanitized_provider_number} to conference {conference_name}")
        try:
            twiml_provider = VoiceResponse()
            dial_provider = Dial()
            dial_provider.conference(
                conference_name,
                start_conference_on_enter=False,  # Provider does not start the conference
                end_conference_on_exit=False,
                beep=False
            )
            twiml_provider.append(dial_provider)
            provider_call = client.calls.create(
                to=sanitized_provider_number,
                from_=self.conversation_state_manager.get_from_phone(),  # Twilio number
                twiml=str(twiml_provider)
            )
            logger.info(f"Provider call initiated with SID {provider_call.sid}")
        except Exception as e:
            logger.error(f"Error adding provider to conference: {e}")
            raise

        # Step 3: Add Supervisor to Conference via New Call
        supervisor_number = sanitize_phone_number(supervisor_phone)
        logger.info(f"Adding supervisor {supervisor_number} to conference {conference_name} as muted")
        try:
            twiml_supervisor = VoiceResponse()
            dial_supervisor = Dial()
            dial_supervisor.conference(
                conference_name,
                start_conference_on_enter=False,  # Supervisor does not start the conference
                end_conference_on_exit=False,
                beep=False
                # Do not set 'muted=True' here; mute via API after joining
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

        # Step 4: Wait for Conference to be Active
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

        # Step 5: Wait for Supervisor to Join and Retrieve Participant SID
        supervisor_participant_sid = None
        max_participant_attempts = 10
        participant_attempt = 0
        while participant_attempt < max_participant_attempts and not supervisor_participant_sid:
            try:
                participants = client.conferences(conference_sid).participants.list()
                for participant in participants:
                    if participant.call_sid == supervisor_call.sid:
                        supervisor_participant_sid = participant.sid
                        logger.info(f"Supervisor's Participant SID found: {supervisor_participant_sid}")
                        break
            except Exception as e:
                logger.error(f"Error retrieving conference participants: {e}")
            if supervisor_participant_sid:
                break
            await asyncio.sleep(1)
            participant_attempt += 1
            logger.info(f"Waiting for supervisor to join the conference. Attempt {participant_attempt}/{max_participant_attempts}")

        if not supervisor_participant_sid:
            logger.warning("Supervisor's Participant SID not found after maximum attempts")
            # Proceed without muting
            return

        # Step 6: Mute Supervisor via REST API
        try:
            client.conferences(conference_sid).participants(supervisor_participant_sid).update(muted=True)
            logger.info(f"Supervisor {supervisor_number} has been muted in conference {conference_name}")
        except Exception as e:
            logger.error(f"Error muting supervisor: {e}")
            # Optionally, proceed without muting
            logger.warning("Proceeding without muting the supervisor due to API failure")


    async def run(
        self, action_input: ActionInput[ListenOnlyWarmTransferCallParameters]
    ) -> ActionOutput[ListenOnlyWarmTransferCallResponse]:
        try:
            twilio_call_sid = self.get_twilio_sid(action_input)
            supervisor_phone = self.action_config.get_phone_number(action_input)
            sanitized_supervisor_phone = sanitize_phone_number(supervisor_phone)

            if action_input.user_message_tracker is not None:
                await action_input.user_message_tracker.wait()

            logger.info(f"Starting listen-only warm transfer to supervisor {sanitized_supervisor_phone}")

            if self.conversation_state_manager.transcript.was_last_message_interrupted():
                logger.info("Last bot message was interrupted, not transferring call")
                return ActionOutput(
                    action_type=action_input.action_config.type,
                    response=ListenOnlyWarmTransferCallResponse(success=False),
                )

            await self.transfer_call(twilio_call_sid, sanitized_supervisor_phone)

            logger.info(f"Listen-only warm transfer to supervisor {sanitized_supervisor_phone} completed successfully")
            return ActionOutput(
                action_type=action_input.action_config.type,
                response=ListenOnlyWarmTransferCallResponse(success=True),
            )
        except Exception as e:
            logger.error(f"Error during listen-only warm transfer: {e}")
            return ActionOutput(
                action_type=action_input.action_config.type,
                response=ListenOnlyWarmTransferCallResponse(success=False),
            )