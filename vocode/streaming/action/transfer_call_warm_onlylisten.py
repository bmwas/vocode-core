from typing import Literal, Optional, Type, Union
from pydantic.v1 import BaseModel, Field
from vocode.streaming.action.phone_call_action import TwilioPhoneConversationAction
from vocode.streaming.models.actions import ActionConfig as VocodeActionConfig
from vocode.streaming.models.actions import ActionInput, ActionOutput
from vocode.streaming.utils.async_requester import AsyncRequestor
from vocode.streaming.utils.phone_numbers import sanitize_phone_number
from vocode.streaming.utils.state_manager import TwilioPhoneConversationStateManager
from loguru import logger

class ListenOnlyWarmTransferCallEmptyParameters(BaseModel):
    pass

class ListenOnlyWarmTransferCallRequiredParameters(BaseModel):
    coach_phone_number: str = Field(
        ..., description="The phone number of the coach to transfer the call to"
    )

ListenOnlyWarmTransferCallParameters = Union[
    ListenOnlyWarmTransferCallEmptyParameters, ListenOnlyWarmTransferCallRequiredParameters
]

class ListenOnlyWarmTransferCallResponse(BaseModel):
    success: bool

class ListenOnlyWarmTransferCallVocodeActionConfig(
    VocodeActionConfig, type="action_listen_only_warm_transfer_call"
):
    coach_phone_number: Optional[str] = Field(
        None, description="The phone number of the coach to transfer the call to"
    )

    def get_coach_phone_number(self, input: ActionInput) -> str:
        if isinstance(input.params, ListenOnlyWarmTransferCallRequiredParameters):
            return input.params.coach_phone_number
        elif isinstance(input.params, ListenOnlyWarmTransferCallEmptyParameters):
            assert self.coach_phone_number, "coach_phone_number must be set"
            return self.coach_phone_number
        else:
            raise TypeError("Invalid input params type")

    def action_attempt_to_string(self, input: ActionInput) -> str:
        coach_phone_number = self.get_coach_phone_number(input)
        return f"Attempting to transfer call to coach at {coach_phone_number}"

    def action_result_to_string(self, input: ActionInput, output: ActionOutput) -> str:
        assert isinstance(output.response, ListenOnlyWarmTransferCallResponse)
        if output.response.success:
            action_description = "Successfully transferred call to coach"
        else:
            action_description = "Did not transfer call because user interrupted"
        return action_description

FUNCTION_DESCRIPTION = """Transfers the call to a coach or supervisor in listen-only mode."""
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
        if self.action_config.coach_phone_number:
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

    async def transfer_call(self, twilio_call_sid: str, coach_phone_number: str):
        twilio_client = self.conversation_state_manager.create_twilio_client()
        account_sid = twilio_client.get_telephony_config().account_sid
        auth = twilio_client.auth
        async_requestor = AsyncRequestor()
        session = async_requestor.get_session()

        transfer_url = f'https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Calls/{twilio_call_sid}.json'

        payload = {
            'Url': os.environ.get("APPLICATION_INBOUND_AUDIO_STREAM_WEBSOCKET"),
            'Method': 'POST',
        }

        async with session.post(transfer_url, data=payload, auth=auth) as response:
            if response.status not in [200, 201]:
                logger.error(f"Failed to transfer call {twilio_call_sid}: {response.status} {response.reason}")
                raise Exception(f"Failed to transfer call {twilio_call_sid}")
            else:
                logger.info(f"Transferred call {twilio_call_sid} to listen-only conference")

        async with session.post(coach_call_url, data=coach_payload, auth=auth) as response:
            if response.status not in [200, 201]:
                logger.error(f"Failed to call coach at {coach_phone_number}: {response.status} {response.reason}")
                raise Exception(f"Failed to call coach at {coach_phone_number}")
            else:
                logger.info(f"Called coach at {coach_phone_number} and added to listen-only conference")

    async def run(
        self, action_input: ActionInput[ListenOnlyWarmTransferCallParameters]
    ) -> ActionOutput[ListenOnlyWarmTransferCallResponse]:
        twilio_call_sid = self.get_twilio_sid(action_input)
        coach_phone_number = self.action_config.get_coach_phone_number(action_input)

        if action_input.user_message_tracker is not None:
            await action_input.user_message_tracker.wait()

            logger.info("Finished waiting for user message tracker, now attempting to transfer call")

            if self.conversation_state_manager.transcript.was_last_message_interrupted():
                logger.info("Last bot message was interrupted, not transferring call")
                return ActionOutput(
                    action_type=action_input.action_config.type,
                    response=ListenOnlyWarmTransferCallResponse(success=False),
                )

        await self.transfer_call(twilio_call_sid, coach_phone_number)

        return ActionOutput(
            action_type=action_input.action_config.type,
            response=ListenOnlyWarmTransferCallResponse(success=True),
        )