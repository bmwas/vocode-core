from typing import Literal, Optional, Type, Union
import asyncio
from loguru import logger
from pydantic.v1 import BaseModel, Field
from vocode.streaming.action.phone_call_action import (
    TwilioPhoneConversationAction,
)
from vocode.streaming.models.actions import ActionConfig as VocodeActionConfig
from vocode.streaming.models.actions import ActionInput, ActionOutput
from vocode.streaming.utils.async_requester import AsyncRequestor
from vocode.streaming.utils.state_manager import (
    TwilioPhoneConversationStateManager,
)
from twilio.rest import Client
from twilio.twiml.voice_response import VoiceResponse, Connect, Stream
import os

class ListenOnlyWarmTransferCallEmptyParameters(BaseModel):
    pass


class ListenOnlyWarmTransferCallRequiredParameters(BaseModel):
    websocket_server_address: str = Field(
        ..., description="The websocket server address to forward the call audio to"
    )
    coach_phone_number: str = Field(
        ..., description="The coach's phone number to call"
    )
    outbound_websocket_server_address: str = Field(
        ..., description="The websocket server address for the coach to receive the call audio"
    )


ListenOnlyWarmTransferCallParameters = Union[
    ListenOnlyWarmTransferCallEmptyParameters, ListenOnlyWarmTransferCallRequiredParameters
]


class ListenOnlyWarmTransferCallResponse(BaseModel):
    success: bool


class ListenOnlyWarmTransferCallVocodeActionConfig(
    VocodeActionConfig, type="action_listen_only_warm_transfer_call"
):  # type: ignore
    websocket_server_address: Optional[str] = Field(
        None, description="The websocket server address to forward the call audio to"
    )
    coach_phone_number: Optional[str] = Field(
        None, description="The coach's phone number to call"
    )
    outbound_websocket_server_address: Optional[str] = Field(
        None, description="The websocket server address for the coach to receive the call audio"
    )

    def get_websocket_server_address(self, input: ActionInput) -> str:
        if isinstance(input.params, ListenOnlyWarmTransferCallRequiredParameters):
            return input.params.websocket_server_address
        elif isinstance(input.params, ListenOnlyWarmTransferCallEmptyParameters):
            assert (
                self.websocket_server_address
            ), "websocket_server_address must be set"
            return self.websocket_server_address
        else:
            raise TypeError("Invalid input params type")

    def get_coach_phone_number(self, input: ActionInput) -> str:
        if isinstance(input.params, ListenOnlyWarmTransferCallRequiredParameters):
            return input.params.coach_phone_number
        elif isinstance(input.params, ListenOnlyWarmTransferCallEmptyParameters):
            assert (
                self.coach_phone_number
            ), "coach_phone_number must be set"
            return self.coach_phone_number
        else:
            raise TypeError("Invalid input params type")

    def get_outbound_websocket_server_address(self, input: ActionInput) -> str:
        if isinstance(input.params, ListenOnlyWarmTransferCallRequiredParameters):
            return input.params.outbound_websocket_server_address
        elif isinstance(input.params, ListenOnlyWarmTransferCallEmptyParameters):
            assert (
                self.outbound_websocket_server_address
            ), "outbound_websocket_server_address must be set"
            return self.outbound_websocket_server_address
        else:
            raise TypeError("Invalid input params type")

    def action_attempt_to_string(self, input: ActionInput) -> str:
        websocket_server_address = self.get_websocket_server_address(input)
        return f"Attempting to start streaming call audio to {websocket_server_address}"

    def action_result_to_string(
        self, input: ActionInput, output: ActionOutput
    ) -> str:
        assert isinstance(output.response, ListenOnlyWarmTransferCallResponse)
        if output.response.success:
            action_description = "Successfully started streaming call audio and called the coach"
        else:
            action_description = "Did not start streaming because user interrupted"
        return action_description


FUNCTION_DESCRIPTION = """Starts streaming the call audio to a websocket server so a coach or supervisor can listen to the ongoing call, and initiates a call to the coach to allow them to listen in."""
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
    response_type: Type[
        ListenOnlyWarmTransferCallResponse
    ] = ListenOnlyWarmTransferCallResponse
    conversation_state_manager: TwilioPhoneConversationStateManager

    @property
    def parameters_type(self) -> Type[ListenOnlyWarmTransferCallParameters]:
        if (
            self.action_config.websocket_server_address
            and self.action_config.coach_phone_number
            and self.action_config.outbound_websocket_server_address
        ):
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

    async def start_stream(self, twilio_call_sid: str, websocket_server_address: str):
        twilio_client = self.conversation_state_manager.create_twilio_client()
        account_sid = twilio_client.get_telephony_config().account_sid
        auth = twilio_client.auth  # Should be a tuple (username, auth_token)
        async_requestor = AsyncRequestor()
        session = async_requestor.get_session()

        # Build the URL to start the stream
        start_stream_url = f'https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Calls/{twilio_call_sid}/Streams.json'

        # Prepare the payload
        payload = {
            'Url': websocket_server_address,
            'Track': 'both_tracks',
        }

        print("Stream URL >>>>>>>>>>>>>>>>",start_stream_url)
        print("Stream Payload >>>>>>>>>>>>>>>>", payload)
        
        async with session.post(start_stream_url, data=payload, auth=auth) as response:
            if response.status not in [200, 201]:
                logger.error(
                    f"Failed to start stream on call {twilio_call_sid}: {response.status} {response.reason}"
                )
                raise Exception(f"Failed to start stream on call {twilio_call_sid}")
            else:
                logger.info(
                    f"Started stream on call {twilio_call_sid} to {websocket_server_address}"
                )

    async def call_coach(self, coach_phone_number: str, outbound_websocket_url: str):
        # Initialize Twilio Client
        twilio_client = self.conversation_state_manager.create_twilio_client()
        account_sid = twilio_client.get_telephony_config().account_sid
        auth_token = twilio_client.get_telephony_config().auth_token
        from_number = twilio_client.get_telephony_config().caller_id

        client = Client(account_sid, auth_token)

        # Create TwiML response
        response = VoiceResponse()
        connect = Connect()
        stream = Stream(url=outbound_websocket_url)
        print("Outbound websocket address >>> ", outbound_websocket_url)
        connect.append(stream)
        response.append(connect)

        # Convert TwiML to string
        twiml = str(response)

        try:
            # Create the call with embedded TwiML
            call = client.calls.create(
                to=coach_phone_number,
                from_= os.environ.get("TWILIO_STREAM_NUMBER"),
                twiml=twiml
            )
            logger.info(f"Call initiated successfully. SID: {call.sid}")
        except Exception as e:
            logger.error(f"Error initiating coach call: {e}")
            raise

    async def run(
        self, action_input: ActionInput[ListenOnlyWarmTransferCallParameters]
    ) -> ActionOutput[ListenOnlyWarmTransferCallResponse]:
        twilio_call_sid = self.get_twilio_sid(action_input)
        websocket_server_address = self.action_config.get_websocket_server_address(
            action_input
        )
        coach_phone_number = self.action_config.get_coach_phone_number(action_input)
        outbound_websocket_server_address = self.action_config.get_outbound_websocket_server_address(
            action_input
        )

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

        # Start streaming to the websocket_server_address
        await self.start_stream(twilio_call_sid, websocket_server_address)

        # Call the coach and have them listen via the outbound_websocket_server_address
        await self.call_coach(coach_phone_number, outbound_websocket_server_address)

        return ActionOutput(
            action_type=action_input.action_config.type,
            response=ListenOnlyWarmTransferCallResponse(success=True),
        )
