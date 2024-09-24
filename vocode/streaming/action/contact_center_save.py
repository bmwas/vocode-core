import re
import json
import os
import secrets
from typing import Type, Optional

from loguru import logger
from pydantic import BaseModel, Field

from vocode.streaming.action.phone_call_action import TwilioPhoneConversationAction
from vocode.streaming.models.actions import ActionConfig as VocodeActionConfig
from vocode.streaming.models.actions import ActionInput, ActionOutput
from vocode.streaming.utils.async_requester import AsyncRequestor
from vocode.streaming.utils.state_manager import TwilioPhoneConversationStateManager

import aiohttp


class SaveContactParameters(BaseModel):
    caller_name: str = Field(..., description="The name of the caller")
    email_address: str = Field(..., description="The email address of the caller")


class SaveContactCenterResponse(BaseModel):
    success: bool
    result: Optional[dict]


class SaveContactToContactCenterActionConfig(
    VocodeActionConfig, type="action_save_contact_center"
):
    def action_attempt_to_string(self, input: ActionInput) -> str:
        return "Attempting to save caller contact information to contact center"

    def action_result_to_string(self, input: ActionInput, output: ActionOutput) -> str:
        if (
            output.response
            and output.response.result
            and "agent_message" in output.response.result
        ):
            return output.response.result["agent_message"]
        elif output.response.success:
            return "Operation was successful, but no message provided."
        else:
            return "Failed to save contact information"


class SaveContactToContactCenterAction(
    TwilioPhoneConversationAction[
        SaveContactToContactCenterActionConfig,
        SaveContactParameters,
        SaveContactCenterResponse,
    ]
):
    description: str = """
    Saves caller information such as name, phone number, and email address to the contact center.
    """
    response_type: Type[SaveContactCenterResponse] = SaveContactCenterResponse
    conversation_state_manager: TwilioPhoneConversationStateManager

    @property
    def parameters_type(self) -> Type[SaveContactParameters]:
        return SaveContactParameters

    def __init__(
        self,
        action_config: SaveContactToContactCenterActionConfig,
    ):
        super().__init__(
            action_config,
            quiet=False,
            is_interruptible=False,
            should_respond="always",
        )

    async def run(
        self, action_input: ActionInput[SaveContactParameters]
    ) -> ActionOutput[SaveContactCenterResponse]:
        twilio_call_sid = self.get_twilio_sid(action_input)
        logger.debug(f"Twilio Call SID: {twilio_call_sid}")

        twilio_client = self.conversation_state_manager.create_twilio_client()

        url = f"https://api.twilio.com/2010-04-01/Accounts/{twilio_client.get_telephony_config().account_sid}/Calls/{twilio_call_sid}.json"

        async with AsyncRequestor().get_session() as session:
            async with session.get(url, auth=twilio_client.auth) as response:
                if response.status != 200:
                    logger.error(
                        f"Failed to get call details: {response.status} {response.reason}"
                    )
                    success = False
                    agent_message = "Failed to get caller details"
                    message = {
                        "result": {"success": False},
                        "agent_message": agent_message,
                    }
                    return ActionOutput(
                        action_type=action_input.action_config.type,
                        response=SaveContactCenterResponse(
                            success=success, result=message
                        ),
                    )
                else:
                    call_details = await response.json()
                    logger.debug(f"Call Details: {call_details}")

        phone_number = call_details.get("from", "")
        logger.debug(f"Extracted Phone Number: {phone_number}")

        if not phone_number:
            logger.error("No phone number found in call details.")
            success = False
            agent_message = "No phone number found in call details"
            message = {
                "result": {"success": False},
                "agent_message": agent_message,
            }

            return ActionOutput(
                action_type=action_input.action_config.type,
                response=SaveContactCenterResponse(success=success, result=message),
            )

        server_url = os.environ.get("PORTAL_URL")
        headers = {
            "Content-Type": "application/json",
            "X-Auth-Token": os.environ.get("PORTAL_AUTH_TOKEN"),
            "X-User-Id": os.environ.get("PORTAL_USER_ID"),
        }

        if (
            not server_url
            or not headers["X-Auth-Token"]
            or not headers["X-User-Id"]
        ):
            logger.error(
                "Missing environment variables for PORTAL_URL, PORTAL_AUTH_TOKEN, or PORTAL_USER_ID."
            )
            success = False
            agent_message = "Failed to save contact information"
            message = {
                "result": {"success": False},
                "agent_message": agent_message,
            }
            return ActionOutput(
                action_type=action_input.action_config.type,
                response=SaveContactCenterResponse(success=success, result=message),
            )

        caller_name = action_input.parameters.caller_name
        email_address = action_input.parameters.email_address

        success, agent_message = await add_to_contact_center(
            server_url, headers, phone_number, caller_name, email_address
        )

        message = {
            "result": {"success": success},
            "agent_message": agent_message,
        }

        logger.debug(f"Final Message: {agent_message}")

        return ActionOutput(
            action_type=action_input.action_config.type,
            response=SaveContactCenterResponse(success=success, result=message),
        )


async def add_to_contact_center(server_url, headers, phone, caller_name, email_address):
    # Normalize phone number
    if phone.startswith("+"):
        normalized_phone = phone
    elif phone.startswith("1"):
        normalized_phone = "+" + phone
    else:
        normalized_phone = "+1" + phone

    logger.debug(f"Normalized Phone Number: {normalized_phone}")

    try:
        # Create a random ID and token
        token = secrets.token_urlsafe(22)
        _id = secrets.token_urlsafe(17)
        data = {
            "_id": _id,
            "token": token,
            "phone": normalized_phone,
            "name": caller_name,
            "email": email_address,
        }
        logger.debug(f"Data to send: {data}")

        async with AsyncRequestor().get_session() as session:
            async with session.post(
                f"{server_url}/api/v1/omnichannel/contact",
                headers=headers,
                json=data,
            ) as r_add:
                if r_add.status != 200:
                    logger.error(
                        f"Failed to add contact: {r_add.status} {r_add.reason}"
                    )
                    return False, "Failed to add contact"
                else:
                    logger.debug("Contact added successfully")
                    return True, "Contact added successfully"
    except Exception as e:
        logger.error(f"Exception during contact add: {e}")
        return False, f"Exception occurred: {e}"