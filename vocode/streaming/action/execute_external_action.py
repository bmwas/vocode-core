import json
from typing import Any, Dict, Optional, Type

from loguru import logger
from pydantic.v1 import BaseModel

from vocode.streaming.action.phone_call_action import TwilioPhoneConversationAction
from vocode.streaming.action.external_actions_requester import (
    ExternalActionResponse,
    ExternalActionsRequester,
)
from vocode.streaming.models.actions import ActionConfig as VocodeActionConfig
from vocode.streaming.models.actions import (
    ActionInput,
    ActionOutput,
    ExternalActionProcessingMode,
)
from vocode.streaming.models.message import BaseMessage
from vocode.streaming.utils.state_manager import TwilioPhoneConversationStateManager
from vocode.streaming.utils.async_requester import AsyncRequestor
from vocode.streaming.utils.phone_numbers import sanitize_phone_number


class ExecuteExternalActionVocodeActionConfig(
    VocodeActionConfig, type="action_external"  # type: ignore
):
    processing_mode: ExternalActionProcessingMode
    name: str
    description: str
    url: str
    input_schema: str
    speak_on_send: bool
    speak_on_receive: bool
    signature_secret: str


class ExecuteExternalActionParameters(BaseModel):
    payload: Dict[str, Any]


class ExecuteExternalActionResponse(BaseModel):
    success: bool
    result: Optional[dict]


class ExecuteExternalAction(
    TwilioPhoneConversationAction[
        ExecuteExternalActionVocodeActionConfig,
        ExecuteExternalActionParameters,
        ExecuteExternalActionResponse,
    ]
):
    parameters_type: Type[ExecuteExternalActionParameters] = ExecuteExternalActionParameters
    response_type: Type[ExecuteExternalActionResponse] = ExecuteExternalActionResponse
    conversation_state_manager: TwilioPhoneConversationStateManager

    def __init__(
        self,
        action_config: ExecuteExternalActionVocodeActionConfig,
        conversation_state_manager: TwilioPhoneConversationStateManager,
    ):
        self.description = action_config.description
        super().__init__(
            action_config,
            conversation_state_manager=conversation_state_manager,
            quiet=not action_config.speak_on_receive,
            should_respond="always" if action_config.speak_on_send else "never",
            is_interruptible=False,
        )
        self.external_actions_requester = ExternalActionsRequester(url=action_config.url)

    def _user_message_param_info(self):
        return {
            "type": "string",
            "description": (
                "A message to reply to the user with BEFORE we make the function call."
                "\nEssentially a live response informing them that the function is "
                "about to happen."
            ),
        }

    def create_action_params(self, params: Dict[str, Any]) -> ExecuteExternalActionParameters:
        return ExecuteExternalActionParameters(payload=params)

    def get_function_name(self) -> str:
        return self.action_config.name

    def get_parameters_schema(self) -> Dict[str, Any]:
        return json.loads(self.action_config.input_schema)

    async def send_external_action_request(
        self, action_input: ActionInput[ExecuteExternalActionParameters]
    ) -> ExternalActionResponse:
        return await self.external_actions_requester.send_request(
            payload=action_input.params.payload,
            signature_secret=self.action_config.signature_secret,
        )

    async def run(
        self, action_input: ActionInput[ExecuteExternalActionParameters]
    ) -> ActionOutput[ExecuteExternalActionResponse]:
        # Pre-processing logic
        if self.should_respond and action_input.user_message_tracker is not None:
            await action_input.user_message_tracker.wait()

        self.conversation_state_manager.mute_agent()

        # Extract Twilio Call SID using inherited method
        twilio_call_sid = self.get_twilio_sid(action_input)
        print ("Twilio Call SID >>>>>>>>>>>>>>>>",twilio_call_sid)
        logger.info(f"Twilio Call SID: {twilio_call_sid}")

        # Send external action request
        response = await self.send_external_action_request(action_input)
        self.conversation_state_manager.unmute_agent()

        # Handle the response
        return ActionOutput(
            action_type=action_input.action_config.type,
            response=ExecuteExternalActionResponse(
                result=response.result if response else None, success=response.success
            ),
            canned_response=(
                BaseMessage(text=response.agent_message)
                if response and response.agent_message
                else None
            ),
        )