from typing import Literal, Optional, Type, Union
import asyncio
from loguru import logger
from pydantic.v1 import BaseModel, Field
from twilio.rest import Client
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

        # Step 1: Create the Conference
        logger.info(f"Creating conference {conference_name}")
        try:
            conference = client.conferences.create(
                friendly_name=conference_name,
                status_callback='http://your-callback-url.com/events',  # Replace with your actual callback URL
                status_callback_event=['start', 'end', 'join', 'leave', 'mute', 'hold'],
                record=False
            )
            logger.info(f"Conference created with SID: {conference.sid}")
        except Exception as e:
            logger.error(f"Error creating conference: {e}")
            raise

        # Step 2: Add Agent to the Conference (without updating their call)
        # Determine Agent's Phone Number based on call direction
        direction = self.conversation_state_manager.get_direction()
        if direction == "outbound":
            agent_phone = self.conversation_state_manager.get_from_phone()
        else:
            agent_phone = self.conversation_state_manager.get_to_phone()

        if not agent_phone:
            logger.error("Agent phone number is not set")
            raise Exception("Agent phone number is not set")

        sanitized_agent_number = sanitize_phone_number(agent_phone)
        logger.info(f"Adding agent {sanitized_agent_number} to conference {conference_name}")
        try:
            agent_participant = client.conferences(conference.sid).participants.create(
                from_=self.conversation_state_manager.get_from_phone(),
                to=sanitized_agent_number,
                early_media=True
            )
            logger.info(f"Agent added to conference with Participant SID: {agent_participant.sid}")
        except Exception as e:
            logger.error(f"Error adding agent to conference: {e}")
            raise

        # Step 3: Add Provider to Conference
        provider_phone = self.conversation_state_manager.get_to_phone() if direction == "outbound" else self.conversation_state_manager.get_from_phone()
        if not provider_phone:
            logger.error("Provider phone number is not set")
            raise Exception("Provider phone number is not set")

        sanitized_provider_number = sanitize_phone_number(provider_phone)
        logger.info(f"Adding provider {sanitized_provider_number} to conference {conference_name}")
        try:
            provider_participant = client.conferences(conference.sid).participants.create(
                from_=self.conversation_state_manager.get_from_phone(),  # Twilio number
                to=sanitized_provider_number,
                early_media=True
            )
            logger.info(f"Provider added to conference with Participant SID: {provider_participant.sid}")
        except Exception as e:
            logger.error(f"Error adding provider to conference: {e}")
            raise

        # Step 4: Add Supervisor to Conference (Initially Unmuted)
        supervisor_number = sanitize_phone_number(supervisor_phone)
        logger.info(f"Adding supervisor {supervisor_number} to conference {conference_name} as a listener")
        try:
            supervisor_participant = client.conferences(conference.sid).participants.create(
                from_=self.conversation_state_manager.get_from_phone(),  # Twilio number
                to=supervisor_number,
                early_media=True
            )
            logger.info(f"Supervisor added to conference with Participant SID: {supervisor_participant.sid}")
        except Exception as e:
            logger.error(f"Error adding supervisor to conference: {e}")
            raise

        # Step 5: Mute Supervisor via REST API
        try:
            client.conferences(conference.sid).participants(supervisor_participant.sid).update(muted=True)
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
            logger.error
