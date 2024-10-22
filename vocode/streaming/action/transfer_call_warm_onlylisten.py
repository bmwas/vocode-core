from typing import Literal, Type
from loguru import logger
from vocode.streaming.action.phone_call_action import TwilioPhoneConversationAction
from vocode.streaming.models.actions import ActionInput, ActionOutput
from vocode.streaming.utils.async_requester import AsyncRequestor
from vocode.streaming.utils.state_manager import TwilioPhoneConversationStateManager
from twilio.rest import Client
from twilio.twiml.voice_response import VoiceResponse, Connect, Stream

class ListenOnlyWarmTransferCallResponse(BaseModel):
    success: bool

FUNCTION_DESCRIPTION = """Allows supervisor or coach to listen to an ongoing call for coaching and quality control purposes."""
QUIET = True
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
    parameters_type: Type[ListenOnlyWarmTransferCallParameters] = ListenOnlyWarmTransferCallParameters
    response_type: Type[
        ListenOnlyWarmTransferCallResponse
    ] = ListenOnlyWarmTransferCallResponse
    conversation_state_manager: TwilioPhoneConversationStateManager

    def __init__(self, action_config: ListenOnlyWarmTransferCallVocodeActionConfig):
        super().__init__(
            action_config,
            quiet=QUIET,
            is_interruptible=IS_INTERRUPTIBLE,
            should_respond=SHOULD_RESPOND,
        )

    async def start_inbound_stream(self, twilio_call_sid: str, inbound_websocket_server_address: str):
        twilio_client = self.conversation_state_manager.create_twilio_client()
        account_sid = twilio_client.get_telephony_config().account_sid
        auth = twilio_client.auth  # Should be a tuple (username, auth_token)
        async_requestor = AsyncRequestor()
        session = async_requestor.get_session()

        # Build the URL to start the stream
        start_stream_url = f'https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Calls/{twilio_call_sid}/Streams.json'

        # Prepare the payload
        payload = {
            'Url': inbound_websocket_server_address,
            'Track': 'both_tracks',
        }

        async with session.post(start_stream_url, data=payload, auth=auth) as response:
            if response.status not in [200, 201]:
                logger.error(
                    f"Failed to start inbound stream on call {twilio_call_sid}: {response.status} {response.reason}"
                )
                raise Exception(f"Failed to start inbound stream on call {twilio_call_sid}")
            else:
                logger.info(
                    f"Started inbound stream on call {twilio_call_sid} to {inbound_websocket_server_address}"
                )

    async def call_coach(self, coach_phone_number: str, outbound_websocket_server_address: str):
        twilio_client = self.conversation_state_manager.create_twilio_client()
        account_sid = twilio_client.get_telephony_config().account_sid
        auth_token = twilio_client.get_telephony_config().auth_token
        twilio_number = twilio_client.get_telephony_config().caller_id

        client = Client(account_sid, auth_token)

        # Create TwiML response
        response = VoiceResponse()
        connect = Connect()
        stream = Stream(url=outbound_websocket_server_address)
        connect.append(stream)
        response.append(connect)

        # Convert TwiML to string
        twiml = str(response)

        try:
            # Create the call with embedded TwiML
            call = client.calls.create(
                to=coach_phone_number,
                from_=twilio_number,
                twiml=twiml
            )
            logger.info(f"Coach call initiated successfully. SID: {call.sid}")
        except Exception as e:
            logger.error(f"Error initiating coach call: {e}")
            raise

    async def run(
        self, action_input: ActionInput[ListenOnlyWarmTransferCallParameters]
    ) -> ActionOutput[ListenOnlyWarmTransferCallResponse]:
        twilio_call_sid = self.get_twilio_sid(action_input)
        inbound_websocket_server_address = action_input.params.inbound_websocket_server_address
        coach_phone_number = action_input.params.coach_phone_number
        outbound_websocket_server_address = action_input.params.outbound_websocket_server_address

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

        # Start streaming to the inbound websocket
        await self.start_inbound_stream(twilio_call_sid, inbound_websocket_server_address)

        # Call the coach and stream to their phone using the outbound websocket
        await self.call_coach(coach_phone_number, outbound_websocket_server_address)

        return ActionOutput(
            action_type=action_input.action_config.type,
            response=ListenOnlyWarmTransferCallResponse(success=True),
        )