from typing import Literal, Optional, Type
import os
from loguru import logger
from pydantic import BaseModel, Field
from vocode.streaming.action.phone_call_action import TwilioPhoneConversationAction
from vocode.streaming.models.actions import ActionConfig as VocodeActionConfig
from vocode.streaming.models.actions import ActionInput, ActionOutput
from vocode.streaming.utils.async_requester import AsyncRequestor
from vocode.streaming.utils.state_manager import TwilioPhoneConversationStateManager
from twilio.rest import Client
from twilio.twiml.voice_response import VoiceResponse, Connect, Stream


class ListenOnlyWarmTransferCallParameters(BaseModel):
    coach_phone_number: str = Field(
        ..., description="The phone number of the coach to call"
    )


class ListenOnlyWarmTransferCallResponse(BaseModel):
    success: bool


class ListenOnlyWarmTransferCallVocodeActionConfig(
    VocodeActionConfig, type="action_listen_only_warm_transfer_call"
):
    def action_attempt_to_string(self, input: ActionInput) -> str:
        coach_phone_number = input.params.coach_phone_number
        return f"Attempting to start streaming call audio to coach at {coach_phone_number}"

    def action_result_to_string(
        self, input: ActionInput, output: ActionOutput
    ) -> str:
        assert isinstance(output.response, ListenOnlyWarmTransferCallResponse)
        if output.response.success:
            action_description = "Successfully started streaming call audio to coach"
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
        return ListenOnlyWarmTransferCallParameters

    def __init__(self, action_config: ListenOnlyWarmTransferCallVocodeActionConfig):
        super().__init__(
            action_config,
            quiet=QUIET,
            is_interruptible=IS_INTERRUPTIBLE,
            should_respond=SHOULD_RESPOND,
        )

    async def start_stream(self, twilio_call_sid: str, coach_phone_number: str):
        twilio_client = self.conversation_state_manager.create_twilio_client()
        account_sid = twilio_client.get_telephony_config().account_sid
        auth = twilio_client.auth  # Should be a tuple (username, auth_token)
        async_requestor = AsyncRequestor()
        session = async_requestor.get_session()

        # Get the inbound websocket server address from environment variable
        websocket_server_address = os.environ.get(
            "APPLICATION_INBOUND_AUDIO_STREAM_WEBSOCKET"
        )
        if not websocket_server_address:
            raise Exception(
                "APPLICATION_INBOUND_AUDIO_STREAM_WEBSOCKET is not set in environment variables"
            )

        # Build the URL to start the stream
        start_stream_url = f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Calls/{twilio_call_sid}/Streams.json"

        # Prepare the payload
        payload = {
            "Url": websocket_server_address,
            "Track": "both_tracks",
        }

        async with session.post(
            start_stream_url, data=payload, auth=auth
        ) as response:
            if response.status not in [200, 201]:
                logger.error(
                    f"Failed to start stream on call {twilio_call_sid}: {response.status} {response.reason}"
                )
                raise Exception(f"Failed to start stream on call {twilio_call_sid}")
            else:
                logger.info(
                    f"Started stream on call {twilio_call_sid} to {websocket_server_address}"
                )

                # Now, proceed to initiate the call to the coach
                ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID")
                AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN")
                if not ACCOUNT_SID or not AUTH_TOKEN:
                    raise Exception(
                        "Twilio ACCOUNT_SID or AUTH_TOKEN not set in environment variables"
                    )

                client = Client(ACCOUNT_SID, AUTH_TOKEN)

                # Create TwiML response
                response = VoiceResponse()
                connect = Connect()
                outbound_websocket_url = os.environ.get(
                    "APPLICATION_OUTBOUND_AUDIO_STREAM_WEBSOCKET"
                )
                if not outbound_websocket_url:
                    raise Exception(
                        "APPLICATION_OUTBOUND_AUDIO_STREAM_WEBSOCKET not set in environment variables"
                    )
                stream = Stream(url=outbound_websocket_url)
                connect.append(stream)
                response.append(connect)

                # Convert TwiML to string
                twiml = str(response)

                try:
                    # Create the call with embedded TwiML
                    call = client.calls.create(
                        to=coach_phone_number,
                        from_=os.environ.get("TWILIO_STREAM_NUMBER"),
                        twiml=twiml,
                    )
                    logger.info(
                        f"Call initiated successfully to coach. SID: {call.sid}"
                    )
                except Exception as e:
                    logger.error(f"Error initiating call to coach: {e}", exc_info=True)

    async def run(
        self, action_input: ActionInput[ListenOnlyWarmTransferCallParameters]
    ) -> ActionOutput[ListenOnlyWarmTransferCallResponse]:
        twilio_call_sid = self.get_twilio_sid(action_input)
        coach_phone_number = action_input.params.coach_phone_number

        if action_input.user_message_tracker is not None:
            await action_input.user_message_tracker.wait()

            logger.info(
                "Finished waiting for user message tracker, now attempting to start streaming"
            )

            if (
                self.conversation_state_manager.transcript.was_last_message_interrupted()
            ):
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