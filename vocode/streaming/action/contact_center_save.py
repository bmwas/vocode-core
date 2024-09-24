import re
import json
import os
import secrets
from typing import Type, Optional

from loguru import logger
from pydantic.v1 import BaseModel, Field

from vocode.streaming.action.phone_call_action import TwilioPhoneConversationAction
from vocode.streaming.models.actions import ActionConfig as VocodeActionConfig
from vocode.streaming.models.actions import ActionInput, ActionOutput
from vocode.streaming.utils.async_requester import AsyncRequestor
from vocode.streaming.utils.state_manager import TwilioPhoneConversationStateManager

import aiohttp

class AddContactParameters(BaseModel):
    name: str = Field(..., description="Name of the contact")
    email: str = Field(..., description="Email address of the contact")

class AddToContactCenterResponse(BaseModel):
    success: bool
    result: Optional[dict]

class AddContactToContactCenterActionConfig(
    VocodeActionConfig, type="action_add_contact_to_contact_center"
):
    def action_attempt_to_string(self, input: ActionInput) -> str:
        return "Attempting to save caller contact information to contact center"

    def action_result_to_string(self, input: ActionInput, output: ActionOutput) -> str:
        # Return the agent_message directly if available
        if output.response and output.response.result and "agent_message" in output.response.result:
            return output.response.result["agent_message"]
        elif output.response.success:
            return "Operation was successful, but no message provided."
        else:
            return "Failed to save contact information"

class AddContactToContactCenterAction(
    TwilioPhoneConversationAction[
        AddContactToContactCenterActionConfig, AddContactParameters, AddToContactCenterResponse
    ]
):
    description: str = """
    Saves caller information such as name, phone number, and email address to the contact center.
    Use this action when the agent knows the caller's name and their email address.
    The phone number will be extracted from the ongoing call using the Twilio SID.
    """
    response_type: Type[AddToContactCenterResponse] = AddToContactCenterResponse
    conversation_state_manager: TwilioPhoneConversationStateManager

    @property
    def parameters_type(self) -> Type[AddContactParameters]:
        return AddContactParameters

    def __init__(
        self,
        action_config: AddContactToContactCenterActionConfig,
    ):
        super().__init__(
            action_config,
            quiet=False,
            is_interruptible=False,
            should_respond="always",
        )

    async def run(
        self, action_input: ActionInput[AddContactParameters]
    ) -> ActionOutput[AddToContactCenterResponse]:
        twilio_call_sid = self.get_twilio_sid(action_input)
        logger.debug(f"Twilio Call SID: {twilio_call_sid}")

        twilio_client = self.conversation_state_manager.create_twilio_client()

        url = f"https://api.twilio.com/2010-04-01/Accounts/{twilio_client.get_telephony_config().account_sid}/Calls/{twilio_call_sid}.json"

        async with AsyncRequestor().get_session() as session:
            async with session.get(url, auth=twilio_client.auth) as response:
                if response.status != 200:
                    logger.error(f"Failed to get call details: {response.status} {response.reason}")
                    success = False
                    agent_message = "Failed to get caller details"
                    message = {
                        "result": {"success": False},
                        "agent_message": agent_message} 
                    return ActionOutput(
                        action_type=action_input.action_config.type,
                        response=AddToContactCenterResponse(success=success, result=message),
                    )
                else:
                    call_details = await response.json()
                    logger.debug(f"Call Details: {call_details}")

        phone_number = call_details.get('from', '')
        logger.debug(f"Extracted Phone Number: {phone_number}")

        if not phone_number:
            logger.error("No phone number found in call details.")
            success = False
            agent_message = "No phone number found in call details"
            message = {
                "result": {"success": False},
                "agent_message": agent_message} 
            
            return ActionOutput(
                action_type=action_input.action_config.type,
                response=AddToContactCenterResponse(success=success, result=message),
            )

        server_url = os.environ.get("PORTAL_URL")
        headers = {
            'Content-Type': 'application/json',
            'X-Auth-Token': os.environ.get("PORTAL_AUTH_TOKEN"),
            'X-User-Id': os.environ.get("PORTAL_USER_ID"),
        }

        if not server_url or not headers['X-Auth-Token'] or not headers['X-User-Id']:
            logger.error("Missing environment variables for PORTAL_URL, PORTAL_AUTH_TOKEN, or PORTAL_USER_ID.")
            success = False
            agent_message = "Failed to save contact information due to missing configuration"
            message = {
                "result": {"success": False},
                "agent_message": agent_message}            
            return ActionOutput(
                action_type=action_input.action_config.type,
                response=AddToContactCenterResponse(success=success, result=message),
            )

        # Prepare contact body using self.parameters
        cbody = {
            "name": self.parameters.name,
            "email": self.parameters.email,
            "phone": phone_number
        }

        contact_response = await add_to_contact_center(server_url, headers, cbody)
        logger.debug(f"Contact Response: {contact_response}")

        if contact_response.get("success"):
            success = True
            agent_message = f"Contact {self.parameters.name} has been successfully added to the contact center."
            message = {
                "result": {"success": True},
                "agent_message": agent_message}
        else:
            success = False
            agent_message = "Failed to add contact to the contact center."
            message = {
                "result": {"success": False},
                "agent_message": agent_message}
        logger.debug(f"Final Message: {agent_message}")
        return ActionOutput(
            action_type=action_input.action_config.type,
            response=AddToContactCenterResponse(success=success, result=message),
        )

async def add_to_contact_center(server_url, headers, cbody):
    phone = cbody["phone"]
    if not phone.startswith('+') and not phone.startswith('1'):
        phone = "+1"+phone
    elif phone.startswith('1'):
        phone = "+"+phone
    else:
        phone = phone
    params = {'phone': phone}

    cnt = None
    try:
        async with AsyncRequestor().get_session() as session:
            async with session.get(f'{server_url}/api/v1/omnichannel/contact.search', headers=headers, params=params) as r_search:
                if r_search.status != 200:
                    logger.error(f"Failed to search contact: {r_search.status} {r_search.reason}")
                else:
                    r_search_json = await r_search.json()
                    cnt = r_search_json.get('contact')
    except Exception as e:
        logger.error(f"Exception during contact search: {e}")

    # If contact doesn't exist, create it
    if not cnt:
        # Create a random id and token
        token = secrets.token_urlsafe(22)
        _id = secrets.token_urlsafe(17)
        data = {
            "_id": _id,
            "token": token,
            "phone": phone,
            "name": cbody["name"],
            "email": cbody["email"]
        }
        try:
            async with AsyncRequestor().get_session() as session:
                async with session.post(
                    f'{server_url}/api/v1/omnichannel/contact',
                    headers=headers,
                    data=json.dumps(data)
                ) as r_add:
                    if r_add.status != 200:
                        logger.error(f"Failed to add contact: {r_add.status} {r_add.reason}")
                        success = False
                    else:
                        success = True
        except Exception as e:
            logger.error(f"Exception during contact addition: {e}")
            success = False
    else:
        # Contact already exists
        success = True

    c_response = {"success": success}
    return c_response