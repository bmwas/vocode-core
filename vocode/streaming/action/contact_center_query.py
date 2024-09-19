from typing import Type

from loguru import logger
from pydantic.v1 import BaseModel

from vocode.streaming.action.phone_call_action import TwilioPhoneConversationAction
from vocode.streaming.models.actions import ActionConfig as VocodeActionConfig
from vocode.streaming.models.actions import ActionInput, ActionOutput
from vocode.streaming.utils.async_requester import AsyncRequestor
from vocode.streaming.utils.state_manager import TwilioPhoneConversationStateManager

import json

# Import aiohttp for asynchronous HTTP requests
import aiohttp
import os

class EmptyParameters(BaseModel):
    pass

class QueryContactCenterResponse(BaseModel):
    contact_info: dict

class GetPhoneAndQueryContactCenterActionConfig(VocodeActionConfig, type="action_get_phone_and_query_contact_center"):
    def action_attempt_to_string(self, input: ActionInput) -> str:
        return "Attempting to get phone number and query contact center"

    def action_result_to_string(self, input: ActionInput, output: ActionOutput) -> str:
        return "Completed querying contact center"

class GetPhoneAndQueryContactCenterAction(
    TwilioPhoneConversationAction[
        GetPhoneAndQueryContactCenterActionConfig, EmptyParameters, QueryContactCenterResponse
    ]
):
    description: str = """Fetches the caller's name, phone number, and email from the contact center at 
                          the start of the call or as needed. Returns the information or "EMPTY" 
                          if the caller is not found."""

    response_type: Type[QueryContactCenterResponse] = QueryContactCenterResponse
    conversation_state_manager: TwilioPhoneConversationStateManager

    @property
    def parameters_type(self) -> Type[EmptyParameters]:
        return EmptyParameters

    def __init__(
        self,
        action_config: GetPhoneAndQueryContactCenterActionConfig,
    ):
        super().__init__(
            action_config,
            quiet=True,
            is_interruptible=False,
            should_respond="always",
        )

    async def run(
        self, action_input: ActionInput[EmptyParameters]
    ) -> ActionOutput[QueryContactCenterResponse]:
        twilio_call_sid = self.get_twilio_sid(action_input)

        twilio_client = self.conversation_state_manager.create_twilio_client()

        url = f"https://api.twilio.com/2010-04-01/Accounts/{twilio_client.get_telephony_config().account_sid}/Calls/{twilio_call_sid}.json"

        async with AsyncRequestor().get_session() as session:
            async with session.get(url, auth=twilio_client.auth) as response:
                if response.status != 200:
                    logger.error(f"Failed to get call details: {response.status} {response.reason}")
                    raise Exception("Failed to get call details")
                else:
                    call_details = await response.json()
        phone_number = call_details['from']  # Adjust if you need the 'to' number
        server_url = os.environ.get("PORTAL_URL")
        headers = {
            'Content-type': 'application/json',
            'X-Auth-Token': os.environ.get("PORTAL_AUTH_TOKEN"),
            'X-User-Id': os.environ.get("PORTAL_USER_ID"),}  
        contact_info = await query_contact_center(server_url, headers, phone_number)
        return ActionOutput(
            action_type=action_input.action_config.type,
            response=QueryContactCenterResponse(contact_info=contact_info),
        )

## function that queries a contact center given a phone number.
async def query_contact_center(server_url, headers, phone):
    # Normalize the phone number
    if phone[0] != '+' and phone[0] != "1":
        phone = "+1" + phone
    elif phone[0] == "1":
        phone = "+" + phone
    else:
        phone = phone

    params = {'phone': phone}

    async with AsyncRequestor().get_session() as session:
        async with session.get(
            f'{server_url}/api/v1/omnichannel/contact.search',
            headers=headers,
            params=params
        ) as r_search:
            if r_search.status != 200:
                logger.error(f"Failed to search contact: {r_search.status} {r_search.reason}")
                contact_info = {
                    "caller name": "EMPTY",
                    "caller phone number": "EMPTY",
                    "caller email addresses": "EMPTY"
                    }               
            else:
                r_search_json = await r_search.json()
                contact = r_search_json.get('contact', {})
                contact_info = {
                    "caller name": contact.get('name', ''),
                    "caller phone number": phone,
                    "caller email addresses": contact.get('visitorEmails', [])
                    }
                if not contact:
                    logger.error("Contact not found")
                    contact_info = {
                    "caller name": "EMPTY",
                    "caller phone number": "EMPTY",
                    "caller email addresses": "EMPTY"
                    }                       
    return contact_info