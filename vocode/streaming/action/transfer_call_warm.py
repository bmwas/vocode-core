from typing import Literal, Optional, Type, Union, get_args
import time
import asyncio

from loguru import logger
from pydantic.v1 import BaseModel, Field

from vocode.streaming.action.phone_call_action import (
    TwilioPhoneConversationAction,
)
from vocode.streaming.models.actions import ActionConfig as VocodeActionConfig
from vocode.streaming.models.actions import ActionInput, ActionOutput
from vocode.streaming.utils.async_requester import AsyncRequestor
from vocode.streaming.utils.phone_numbers import sanitize_phone_number
from vocode.streaming.utils.state_manager import (
    TwilioPhoneConversationStateManager,
)


class WarmTransferCallEmptyParameters(BaseModel):
    pass


class WarmTransferCallRequiredParameters(BaseModel):
    phone_number: str = Field(..., description="The phone number to transfer the call to")


WarmTransferCallParameters = Union[
    WarmTransferCallEmptyParameters, WarmTransferCallRequiredParameters
]


class WarmTransferCallResponse(BaseModel):
    success: bool


class WarmTransferCallVocodeActionConfig(
    VocodeActionConfig, type="action_warm_transfer_call"
):  # type: ignore
    phone_number: Optional[str] = Field(
        None, description="The phone number to transfer the call to"
    )

    def get_phone_number(self, input: ActionInput) -> str:
        if isinstance(input.params, WarmTransferCallRequiredParameters):
            return input.params.phone_number
        elif isinstance(input.params, WarmTransferCallEmptyParameters):
            assert self.phone_number, "phone number must be set"
            return self.phone_number
        else:
            raise TypeError("Invalid input params type")

    def action_attempt_to_string(self, input: ActionInput) -> str:
        phone_number = self.get_phone_number(input)
        return f"Attempting to warm transfer call to {phone_number}"

    def action_result_to_string(self, input: ActionInput, output: ActionOutput) -> str:
        assert isinstance(output.response, WarmTransferCallResponse)
        if output.response.success:
            action_description = "Successfully performed warm transfer of call"
        else:
            action_description = "Did not transfer call because user interrupted"
        return action_description


FUNCTION_DESCRIPTION = """Performs a warm transfer of the call to a manager or supervisor by adding all parties to a conference call."""

QUIET = False
IS_INTERRUPTIBLE = True
SHOULD_RESPOND: Literal["always"] = "always"


class TwilioWarmTransferCall(
    TwilioPhoneConversationAction[
        WarmTransferCallVocodeActionConfig, WarmTransferCallParameters, WarmTransferCallResponse
    ]
):
    description: str = FUNCTION_DESCRIPTION
    response_type: Type[WarmTransferCallResponse] = WarmTransferCallResponse
    conversation_state_manager: TwilioPhoneConversationStateManager

    @property
    def parameters_type(self) -> Type[WarmTransferCallParameters]:
        if self.action_config.phone_number:
            return WarmTransferCallEmptyParameters
        else:
            return WarmTransferCallRequiredParameters

    def __init__(
        self,
        action_config: WarmTransferCallVocodeActionConfig,
    ):
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
        async_requestor = AsyncRequestor()

        # Create a unique conference name
        conference_name = f'Conference_{twilio_call_sid}_{int(time.time())}'

        # Use the session as a context manager
        async with async_requestor.get_session() as session:
            # Step 1: Update the existing call (customer and agent) to join the conference
            update_call_url = f'https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Calls/{twilio_call_sid}.json'
            update_payload = {
                'Twiml': f'<Response><Dial><Conference>{conference_name}</Conference></Dial></Response>'
            }
            async with session.post(update_call_url, data=update_payload, auth=auth) as response:
                update_response_text = await response.text()
                if response.status not in [200, 201]:
                    logger.error(f"Failed to update call: {response.status} {response.reason} {update_response_text}")
                    raise Exception("Failed to update call")
                else:
                    logger.info(f"Call {twilio_call_sid} updated to join conference {conference_name}")

            # Step 2: Wait for the conference to be created
            await asyncio.sleep(1)  # Adjust delay as necessary

            # Step 3: Fetch the conference SID using the conference name
            fetch_conference_url = f'https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Conferences.json?FriendlyName={conference_name}'
            async with session.get(fetch_conference_url, auth=auth) as response:
                fetch_response_text = await response.text()
                if response.status != 200:
                    logger.error(f"Failed to fetch conference: {response.status} {response.reason} {fetch_response_text}")
                    raise Exception("Failed to fetch conference")
                data = await response.json()
                conferences = data.get('conferences', [])
                if not conferences:
                    logger.error("No conference found with the specified name")
                    raise Exception("Conference not found")
                conference_sid = conferences[0]['sid']

            # Step 4: Get the agent's phone number
            if self.conversation_state_manager.get_direction() == "outbound":
                agent_phone_number = self.conversation_state_manager.get_from_phone()
            else:
                agent_phone_number = self.conversation_state_manager.get_to_phone()

            # Step 5: Add the manager to the conference by making a call to them
            add_participant_url = f'https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Calls.json'
            participant_payload = {
                'From': agent_phone_number,
                'To': to_phone,
                'Url': f'https://handler.twilio.com/twiml/EHXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX?ConferenceName={conference_name}'
            }
            async with session.post(add_participant_url, data=participant_payload, auth=auth) as response:
                participant_response_text = await response.text()
                if response.status not in [200, 201]:
                    logger.error(f"Failed to call participant: {response.status} {response.reason} {participant_response_text}")
                    raise Exception("Failed to call participant")
                else:
                    logger.info(f"Called participant {to_phone} to join conference {conference_name}")
                    participant_data = await response.json()
                    participant_call_sid = participant_data.get('sid')

        return participant_call_sid

    async def run(
        self, action_input: ActionInput[WarmTransferCallParameters]
    ) -> ActionOutput[WarmTransferCallResponse]:
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
                response=WarmTransferCallResponse(success=False),
            )

        await self.transfer_call(twilio_call_sid, sanitized_phone_number)

        return ActionOutput(
            action_type=action_input.action_config.type,
            response=WarmTransferCallResponse(success=True),
        )