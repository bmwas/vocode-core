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
        auth = twilio_client.auth

        # Create a unique conference name
        conference_name = f'Conference_{twilio_call_sid}_{int(time.time())}'

        # Step 1: Create the conference
        try:
            conference = twilio_client.conferences.create(
                friendly_name=conference_name,
                record=False
            )
            logger.info(f"Created conference: {conference.sid}")
        except Exception as e:
            logger.error(f"Failed to create conference: {str(e)}")
            raise

        # Step 2: Add the agent to the conference
        try:
            agent_participant = twilio_client.conferences(conference.sid).participants.create(
                from_=self.conversation_state_manager.get_from_phone(),
                to=twilio_call_sid,
                start_conference_on_enter=True,
                end_conference_on_exit=False
            )
            logger.info(f"Added agent to conference: {agent_participant.call_sid}")
        except Exception as e:
            logger.error(f"Failed to add agent to conference: {str(e)}")
            raise

        # Step 3: Add the customer to the conference
        try:
            customer_call = twilio_client.calls(twilio_call_sid).fetch()
            customer_participant = twilio_client.conferences(conference.sid).participants.create(
                from_=self.conversation_state_manager.get_from_phone(),
                to=customer_call.to,
                start_conference_on_enter=True,
                end_conference_on_exit=False
            )
            logger.info(f"Added customer to conference: {customer_participant.call_sid}")
        except Exception as e:
            logger.error(f"Failed to add customer to conference: {str(e)}")
            raise

        # Step 4: Add the supervisor to the conference as a coach
        try:
            supervisor_participant = twilio_client.conferences(conference.sid).participants.create(
                from_=self.conversation_state_manager.get_from_phone(),
                to=to_phone,
                coaching=agent_participant.call_sid,
                start_conference_on_enter=False,
                end_conference_on_exit=False
            )
            logger.info(f"Added supervisor to conference as coach: {supervisor_participant.call_sid}")
        except Exception as e:
            logger.error(f"Failed to add supervisor to conference: {str(e)}")
            raise

        return supervisor_participant.call_sid

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

        try:
            supervisor_call_sid = await self.transfer_call(twilio_call_sid, sanitized_phone_number)
            
            return ActionOutput(
                action_type=action_input.action_config.type,
                response=ListenOnlyWarmTransferCallResponse(success=True, supervisor_call_sid=supervisor_call_sid),
            )
        except Exception as e:
            logger.error(f"Failed to transfer call: {str(e)}")
            return ActionOutput(
                action_type=action_input.action_config.type,
                response=ListenOnlyWarmTransferCallResponse(success=False, error_message=str(e)),
            )