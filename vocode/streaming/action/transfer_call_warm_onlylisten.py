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
from twilio.twiml.voice_response import VoiceResponse, Connect, Stream
import sys
import os

class ListenOnlyWarmTransferCallEmptyParameters(BaseModel):
    pass


class ListenOnlyWarmTransferCallRequiredParameters(BaseModel):
    coach_phone_number: str = Field(
        ..., description="The phone number to forward the call audio to"
    )


ListenOnlyWarmTransferCallParameters = Union[
    ListenOnlyWarmTransferCallEmptyParameters, ListenOnlyWarmTransferCallRequiredParameters
]


class ListenOnlyWarmTransferCallResponse(BaseModel):
    success: bool


class ListenOnlyWarmTransferCallVocodeActionConfig(
    VocodeActionConfig, type="action_listen_only_warm_transfer_call"
):  # type: ignore
    coach_phone_number: Optional[str] = Field(
        None, description="The phone number to forward the call audio to"
    )

    def get_coach_phone_number(self, input: ActionInput) -> str:
        if isinstance(input.params, ListenOnlyWarmTransferCallRequiredParameters):
            print("Passed phone >>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>",input.params.coach_phone_number)
            return input.params.coach_phone_number
        elif isinstance(input.params, ListenOnlyWarmTransferCallEmptyParameters):
            assert (
                self.coach_phone_number
            ), "coach_phone_number must be set"
            return self.coach_phone_number
        else:
            raise TypeError("Invalid input params type")

    def action_attempt_to_string(self, input: ActionInput) -> str:
        coach_phone_number = self.get_coach_phone_number(input)
        return f"Attempting to start streaming call audio to {coach_phone_number}"

    def action_result_to_string(
        self, input: ActionInput, output: ActionOutput
    ) -> str:
        assert isinstance(output.response, ListenOnlyWarmTransferCallResponse)
        if output.response.success:
            action_description = "Successfully started streaming call audio"
        else:
            action_description = "Did not start streaming because user interrupted"
        return action_description


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
    response_type: Type[
        ListenOnlyWarmTransferCallResponse
    ] = ListenOnlyWarmTransferCallResponse
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

    async def start_stream(self, twilio_call_sid: str, coach_phone_number: str):
        """
        Starts streaming the call audio to the coach's phone number.

        Args:
            twilio_call_sid (str): The Twilio Call SID of the current call.
            coach_phone_number (str): The coach's phone number.

        Raises:
            Exception: If starting the stream fails.
        """
        logger.debug(f"[START_STREAM METHOD] Starting stream with coach_phone_number: {coach_phone_number}")
        twilio_client = self.conversation_state_manager.create_twilio_client()
        account_sid = twilio_client.get_telephony_config().account_sid
        auth = twilio_client.auth  # Should be a tuple (username, auth_token)

        async_requestor = AsyncRequestor()
        async with async_requestor.get_session() as session:
            # Build the URL to start the stream
            start_stream_url = f'https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Calls/{twilio_call_sid}/Streams.json'
            # Prepare the payload
            payload = {
                'Url': os.environ.get("APPLICATION_INBOUND_AUDIO_STREAM_WEBSOCKET"),
                'Track': 'both_tracks',
            }
            logger.debug(f"[START_STREAM METHOD] Starting stream for call SID {twilio_call_sid} with payload: {payload}")
            async with session.post(start_stream_url, data=payload, auth=auth) as http_response:
                if http_response.status not in [200, 201]:
                    error_body = await http_response.text()
                    logger.error(
                        f"Failed to start stream on call {twilio_call_sid}: {http_response.status} {http_response.reason} - {error_body}"
                    )
                    raise Exception(f"Failed to start stream on call {twilio_call_sid}")
                else:
                    logger.info(
                        f"Started stream on call {twilio_call_sid}"
                    )

        # Now, place a call to the coach's phone number with appropriate TwiML
        # Since the Twilio client is synchronous, run it in an executor
        ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID")
        AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN")
        TWILIO_STREAM_NUMBER = os.environ.get("TWILIO_STREAM_NUMBER")
        OUTBOUND_AUDIO_STREAM_WEBSOCKET = os.environ.get("APPLICATION_OUTBOUND_AUDIO_STREAM_WEBSOCKET")

        # Log environment variables for debugging
        logger.debug(f"Twilio Credentials - ACCOUNT_SID: {ACCOUNT_SID}, TWILIO_STREAM_NUMBER: {TWILIO_STREAM_NUMBER}")
        logger.debug(f"Stream URLs - INBOUND: {os.environ.get('APPLICATION_INBOUND_AUDIO_STREAM_WEBSOCKET')}, OUTBOUND: {OUTBOUND_AUDIO_STREAM_WEBSOCKET}")

        if not all([ACCOUNT_SID, AUTH_TOKEN, TWILIO_STREAM_NUMBER, OUTBOUND_AUDIO_STREAM_WEBSOCKET]):
            logger.error("Missing required environment variables for Twilio configuration.")
            raise Exception("Missing required environment variables for Twilio configuration.")

        # Create the TwiML response
        voice_response = VoiceResponse()
        connect = Connect()
        stream = Stream(url=OUTBOUND_AUDIO_STREAM_WEBSOCKET)
        connect.append(stream)
        voice_response.append(connect)
        twiml = str(voice_response)
        logger.debug(f"Generated TwiML for coach call: {twiml}")

        def make_call(coach_phone_number: str):
            logger.debug(f"[MAKE_CALL FUNCTION] Making call to coach_phone_number: {coach_phone_number}")
            client = Client(ACCOUNT_SID, AUTH_TOKEN)
            coach_call = client.calls.create(
                to=coach_phone_number,
                from_=TWILIO_STREAM_NUMBER,
                twiml=twiml
            )
            logger.info(f"Placed call to coach at {coach_phone_number} with Call SID: {coach_call.sid}")
            return coach_call

    async def run(
        self, action_input: ActionInput[ListenOnlyWarmTransferCallParameters]
    ) -> ActionOutput[ListenOnlyWarmTransferCallResponse]:
        twilio_call_sid = self.get_twilio_sid(action_input)
        coach_phone_number = self.action_config.get_coach_phone_number(
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

        await self.start_stream(twilio_call_sid, coach_phone_number)

        return ActionOutput(
            action_type=action_input.action_config.type,
            response=ListenOnlyWarmTransferCallResponse(success=True),
        )