from typing import Literal, Optional, Type, Union
import os
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
from vocode.streaming.models.actions import FunctionCallActionTrigger


# --- Parameter Models ---

class ListenOnlyWarmTransferCallEmptyParameters(BaseModel):
    pass


class ListenOnlyWarmTransferCallRequiredParameters(BaseModel):
    websocket_server_address: str = Field(
        ..., description="The websocket server address to forward the call audio to"
    )


class ListenOnlyWarmTransferCallExtendedParameters(BaseModel):
    websocket_server_address: str = Field(
        ..., description="The websocket server address to forward the call audio to"
    )
    outbound_websocket_server_address: str = Field(
        ..., description="The outbound websocket server address to forward the call audio to"
    )
    coach_phone_number: str = Field(
        ..., description="The coach's phone number to be used for the call"
    )


ListenOnlyWarmTransferCallParameters = Union[
    ListenOnlyWarmTransferCallEmptyParameters,
    ListenOnlyWarmTransferCallRequiredParameters,
    ListenOnlyWarmTransferCallExtendedParameters,
]


# --- Response Model ---

class ListenOnlyWarmTransferCallResponse(BaseModel):
    success: bool


# --- Action Configuration ---

class ListenOnlyWarmTransferCallVocodeActionConfig(
    VocodeActionConfig, type="action_listen_only_warm_transfer_call"
):  # type: ignore
    websocket_server_address: Optional[str] = Field(
        None, description="The websocket server address to forward the call audio to"
    )
    outbound_websocket_server_address: Optional[str] = Field(
        None, description="The outbound websocket server address to forward the call audio to"
    )
    coach_phone_number: Optional[str] = Field(
        None, description="The coach's phone number to be used for the call"
    )

    def get_websocket_server_address(self, input: ActionInput) -> str:
        if isinstance(input.params, ListenOnlyWarmTransferCallRequiredParameters):
            return input.params.websocket_server_address
        elif isinstance(input.params, ListenOnlyWarmTransferCallExtendedParameters):
            return input.params.websocket_server_address
        elif isinstance(input.params, ListenOnlyWarmTransferCallEmptyParameters):
            assert (
                self.websocket_server_address
            ), "websocket_server_address must be set"
            return self.websocket_server_address
        else:
            raise TypeError("Invalid input params type")

    def get_outbound_websocket_server_address(self, input: ActionInput) -> str:
        if isinstance(input.params, ListenOnlyWarmTransferCallExtendedParameters):
            return input.params.outbound_websocket_server_address
        elif self.outbound_websocket_server_address:
            return self.outbound_websocket_server_address
        else:
            raise ValueError("outbound_websocket_server_address must be set")

    def get_coach_phone_number(self, input: ActionInput) -> str:
        if isinstance(input.params, ListenOnlyWarmTransferCallExtendedParameters):
            return input.params.coach_phone_number
        elif self.coach_phone_number:
            return self.coach_phone_number
        else:
            raise ValueError("coach_phone_number must be set")

    def action_attempt_to_string(self, input: ActionInput) -> str:
        websocket_server_address = self.get_websocket_server_address(input)
        outbound_websocket_server_address = self.get_outbound_websocket_server_address(input)
        coach_phone_number = self.get_coach_phone_number(input)
        return (
            f"Attempting to start streaming call audio to {websocket_server_address}, "
            f"outbound to {outbound_websocket_server_address}, with coach {coach_phone_number}"
        )

    def action_result_to_string(
        self, input: ActionInput, output: ActionOutput
    ) -> str:
        assert isinstance(output.response, ListenOnlyWarmTransferCallResponse)
        if output.response.success:
            action_description = "Successfully started streaming call audio"
        else:
            action_description = "Did not start streaming because user interrupted"
        return action_description


# --- Action Class ---

FUNCTION_DESCRIPTION = """Starts streaming the call audio to a websocket server so a coach or supervisor can listen to the ongoing call."""
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
        if (
            self.action_config.websocket_server_address
            and self.action_config.outbound_websocket_server_address
            and self.action_config.coach_phone_number
        ):
            return ListenOnlyWarmTransferCallEmptyParameters
        else:
            return ListenOnlyWarmTransferCallExtendedParameters

    def __init__(self, action_config: ListenOnlyWarmTransferCallVocodeActionConfig):
        super().__init__(
            action_config,
            quiet=QUIET,
            is_interruptible=IS_INTERRUPTIBLE,
            should_respond=SHOULD_RESPOND,
        )

    async def start_stream(
        self,
        twilio_call_sid: str,
        websocket_server_address: str,
        outbound_websocket_server_address: str,
        coach_phone_number: str,
    ):
        twilio_client = self.conversation_state_manager.create_twilio_client()
        account_sid = twilio_client.get_telephony_config().account_sid
        auth = twilio_client.auth  # Should be a tuple (username, auth_token)
        async_requestor = AsyncRequestor()
        session = async_requestor.get_session()

        # Start inbound stream
        inbound_stream_url = f'https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Calls/{twilio_call_sid}/Streams.json'
        inbound_payload = {
            'Url': websocket_server_address,
            'Track': 'both_tracks',
        }
        print("Websocket Server Address >>>>>>>>>>>>>>>>>> ", websocket_server_address)
        async with session.post(inbound_stream_url, data=inbound_payload, auth=auth) as response:
            if response.status not in [200, 201]:
                logger.error(
                    f"Failed to start inbound stream on call {twilio_call_sid}: {response.status} {response.reason}"
                )
                raise Exception(f"Failed to start inbound stream on call {twilio_call_sid}")
            else:
                logger.info(
                    f"Started inbound stream on call {twilio_call_sid} to {websocket_server_address}"
                )

        # Start outbound stream
        outbound_stream_url = f'https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Calls/{twilio_call_sid}/Streams.json'
        outbound_payload = {
            'Url': outbound_websocket_server_address,
            'Track': 'both_tracks',
        }

        async with session.post(outbound_stream_url, data=outbound_payload, auth=auth) as response:
            if response.status not in [200, 201]:
                logger.error(
                    f"Failed to start outbound stream on call {twilio_call_sid}: {response.status} {response.reason}"
                )
                raise Exception(f"Failed to start outbound stream on call {twilio_call_sid}")
            else:
                logger.info(
                    f"Started outbound stream on call {twilio_call_sid} to {outbound_websocket_server_address}"
                )

        # Optionally, handle coach phone number (e.g., initiate a call to the coach)
        # This part depends on your specific requirements and Twilio setup
        # Example:
        # coach_call_url = f'https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Calls.json'
        # coach_payload = {
        #     'To': coach_phone_number,
        #     'From': YOUR_TWILIO_PHONE_NUMBER,
        #     'Url': 'http://your-application.com/coach_callback',  # TwiML instructions
        # }
        # async with session.post(coach_call_url, data=coach_payload, auth=auth) as response:
        #     if response.status not in [200, 201]:
        #         logger.error(
        #             f"Failed to initiate call to coach {coach_phone_number}: {response.status} {response.reason}"
        #         )
        #         raise Exception(f"Failed to initiate call to coach {coach_phone_number}")
        #     else:
        #         logger.info(
        #             f"Initiated call to coach {coach_phone_number}"
        #         )

    async def run(
        self, action_input: ActionInput[ListenOnlyWarmTransferCallParameters]
    ) -> ActionOutput[ListenOnlyWarmTransferCallResponse]:
        twilio_call_sid = self.get_twilio_sid(action_input)
        websocket_server_address = self.action_config.get_websocket_server_address(action_input)
        outbound_websocket_server_address = self.action_config.get_outbound_websocket_server_address(action_input)
        coach_phone_number = self.action_config.get_coach_phone_number(action_input)

        if action_input.user_message_tracker is not None:
            await action_input.user_message_tracker.wait()

            logger.info(
                "Finished waiting for user message tracker, now attempting to start streaming"
            )

            if self.conversation_state_manager.transcript.was_last_message_interrupted():
                logger.info("Last bot message was interrupted, not starting stream")
                return ActionOutput(
                    action_type=action_input.action_config.type,
                    response=ListenOnlyWarmTransferCallResponse(success=False),
                )

        await self.start_stream(
            twilio_call_sid,
            websocket_server_address,
            outbound_websocket_server_address,
            coach_phone_number,
        )

        return ActionOutput(
            action_type=action_input.action_config.type,
            response=ListenOnlyWarmTransferCallResponse(success=True),
        )


