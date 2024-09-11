import logging
import os
from typing import Type
from pydantic import BaseModel, Field
from vocode.streaming.action.base_action import BaseAction
from vocode.streaming.models.actions import (ActionConfig, ActionInput,
                                             ActionOutput)

from twilio.rest import Client

from .custom_models import MyActionType

class TwilioSendSmsActionConfig(ActionConfig, type="action_send_sms"):
    pass


class TwilioSendSmsParameters(BaseModel):
    to: str = Field(..., description="The mobile number of the recipient.")
    body: str = Field(..., description="The body of the sms.")


class TwilioSendSmsResponse(BaseModel):
    success: bool
    message: str


class TwilioSendSms(
    BaseAction[
        TwilioSendSmsActionConfig, TwilioSendSmsParameters, TwilioSendSmsResponse
    ]
):
    description: str = "Sends an sms or text message."
    parameters_type: Type[TwilioSendSmsParameters] = TwilioSendSmsParameters
    response_type: Type[TwilioSendSmsResponse] = TwilioSendSmsResponse
# TODO: add loggers
    async def run(
        self, action_input: ActionInput[TwilioSendSmsParameters]
    ) -> ActionOutput[TwilioSendSmsResponse]:

        account_sid = os.getenv("TWILIO_ACCOUNT_SID")
        auth_token = os.getenv("TWILIO_AUTH_TOKEN")
        from_number = os.getenv("OUTBOUND_CALLER_NUMBER")
        try:
            client = Client(account_sid, auth_token)
            logging.info(
                f"Sending SMS to: {action_input.params.to}, Body: {action_input.params.body}"
            )
            # Send the sms
            message = client.messages.create(
                from_=from_number,
                body=action_input.params.body,
                to="+1{}".format(action_input.params.to),
            )
            return ActionOutput(
                action_type=self.action_config.type,
                response=TwilioSendSmsResponse(
                    success=True, message="Successfully sent SMS."
                ),
            )

        # TODO: replace bare exception with specific exception
        except RuntimeError as e:
            print(f"Failed to send SMS: {e}")
            return ActionOutput(
                action_type=self.action_config.type,
                response=TwilioSendSmsResponse(
                    success=False, message="Failed to send SMS"
                ),
            )