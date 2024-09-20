from typing import Type

from loguru import logger
from pydantic.v1 import BaseModel

from vocode.streaming.action.phone_call_action import TwilioPhoneConversationAction
from vocode.streaming.models.actions import ActionConfig as VocodeActionConfig
from vocode.streaming.models.actions import ActionInput, ActionOutput
from vocode.streaming.utils.async_requester import AsyncRequestor
from vocode.streaming.utils.state_manager import TwilioPhoneConversationStateManager

import json
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
    description: str = """Searches for caller's name, phone number, and email addresses in the contact center."""

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
        logger.debug(f"Twilio Call SID: {twilio_call_sid}")

        twilio_client = self.conversation_state_manager.create_twilio_client()

        url = f"https://api.twilio.com/2010-04-01/Accounts/{twilio_client.get_telephony_config().account_sid}/Calls/{twilio_call_sid}.json"

        async with AsyncRequestor().get_session() as session:
            async with session.get(url, auth=twilio_client.auth) as response:
                if response.status != 200:
                    logger.error(f"Failed to get call details: {response.status} {response.reason}")
                    raise Exception("Failed to get call details")
                else:
                    call_details = await response.json()
                    logger.debug(f"Call Details: {call_details}")

        phone_number = call_details.get('from', '')
        logger.debug(f"Extracted Phone Number: {phone_number}")

        if not phone_number:
            logger.error("No phone number found in call details.")
            contact_info = {
                "name": "EMPTY",
                "phone_number": "EMPTY",
                "email_addresses": "EMPTY"
            }
            return ActionOutput(
                action_type=action_input.action_config.type,
                response=QueryContactCenterResponse(contact_info=contact_info),
            )

        server_url = os.environ.get("PORTAL_URL")
        headers = {
            'Content-Type': 'application/json',
            'X-Auth-Token': os.environ.get("PORTAL_AUTH_TOKEN"),
            'X-User-Id': os.environ.get("PORTAL_USER_ID"),
        }

        if not server_url or not headers['X-Auth-Token'] or not headers['X-User-Id']:
            logger.error("Missing environment variables for PORTAL_URL, PORTAL_AUTH_TOKEN, or PORTAL_USER_ID.")
            contact_info = {
                "name": "EMPTY",
                "phone_number": "EMPTY",
                "email_addresses": "EMPTY"
            }
            return ActionOutput(
                action_type=action_input.action_config.type,
                response=QueryContactCenterResponse(contact_info=contact_info),
            )

        contact_info = await query_contact_center(server_url, headers, phone_number)
        logger.debug(f"Contact Info Retrieved: {contact_info}")

        return ActionOutput(
            action_type=action_input.action_config.type,
            response=QueryContactCenterResponse(contact_info=contact_info),
        )


## Function that queries a contact center given a phone number.
async def query_contact_center(server_url, headers, phone):
    # Normalize the phone number
    if phone.startswith('+'):
        normalized_phone = phone
    elif phone.startswith('1'):
        normalized_phone = "+" + phone
    else:
        normalized_phone = "+1" + phone

    logger.debug(f"Normalized Phone Number: {normalized_phone}")

    params = {'phone': normalized_phone}

    try:
        async with AsyncRequestor().get_session() as session:
            async with session.get(
                f'{server_url}/api/v1/omnichannel/contact.search',
                headers=headers,
                params=params
            ) as r_search:
                if r_search.status != 200:
                    logger.error(f"Failed to search contact: {r_search.status} {r_search.reason}")
                    contact_info = {
                        "name": "EMPTY",
                        "phone_number": "EMPTY",
                        "email_addresses": "EMPTY"
                    }
                else:
                    r_search_json = await r_search.json()
                    contact = r_search_json.get('contact', {})

                    if not contact:
                        logger.error("Contact not found")
                        contact_info = {
                            "name": "EMPTY",
                            "phone_number": "EMPTY",
                            "email_addresses": "EMPTY"
                        }
                    else:
                        contact_info = {
                            "name": contact.get('name', 'EMPTY') or "EMPTY",
                            "phone_number": normalized_phone,
                            "email_addresses": contact.get('visitorEmails', []) or "EMPTY"
                        }
    except Exception as e:
        logger.error(f"Exception during contact search: {e}")
        contact_info = {
            "name": "EMPTY",
            "phone_number": "EMPTY",
            "email_addresses": "EMPTY"
        }

    logger.debug(f"Final Contact Info: {contact_info}")
    return contact_info