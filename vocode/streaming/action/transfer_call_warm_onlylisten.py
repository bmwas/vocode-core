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
from twilio.rest import Client

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
        
        # Create a unique conference name
        conference_name = f'Conference_{twilio_call_sid}_{int(time.time())}'

        # Use the Twilio REST API client
        from twilio.rest import Client
        twilio_rest_client = Client(auth[0], auth[1], account_sid)

        # Get the Twilio number to use
        import os
        
        twilio_number = os.environ.get("OUTBOUND_CALLER_NUMBER")
        if not twilio_number:
            logger.error("Twilio 'From' phone number is not set")
            raise Exception("Twilio 'From' phone number is not set")

        # Agent is already connected to the conference via initial call
        agent_call_sid = twilio_call_sid  # The agent's Call SID

        # Dial the provider into the conference
        provider_phone_number = self.conversation_state_manager.get_to_phone()
        provider_call = twilio_rest_client.calls.create(
            to=provider_phone_number,
            from_=twilio_number,
            url=f'https://your-server.com/join_conference?ConferenceName={conference_name}',
            method='POST'
        )
        logger.info(f"Provider {provider_phone_number} dialed into conference {conference_name} with Call SID {provider_call.sid}")

        # Add the supervisor as a coach
        supervisor_phone_number = to_phone
        try:
            participant = twilio_rest_client.conferences(conference_name).participants.create(
                from_=twilio_number,
                to=supervisor_phone_number,
                coach=True,
                call_sid_to_coach=agent_call_sid
            )
            logger.info(f"Supervisor {supervisor_phone_number} added as coach to conference {conference_name} with Call SID {participant.call_sid}")
        except Exception as e:
            logger.error(f"Failed to add supervisor as coach: {str(e)}")
            raise Exception("Failed to add supervisor as coach")

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
