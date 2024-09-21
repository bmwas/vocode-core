import re
import json
import os
from typing import Type, Optional, Union, Literal

from loguru import logger
from pydantic.v1 import BaseModel, Field

from vocode.streaming.action.phone_call_action import TwilioPhoneConversationAction
from vocode.streaming.models.actions import ActionConfig as VocodeActionConfig
from vocode.streaming.models.actions import ActionInput, ActionOutput
from vocode.streaming.utils.async_requester import AsyncRequestor
from vocode.streaming.utils.state_manager import TwilioPhoneConversationStateManager

import aiohttp


class EmptyParameters(BaseModel):
    pass


class GetPhoneAndQueryContactCenterActionConfig(
    VocodeActionConfig, type="action_get_phone_and_query_contact_center"
):
    def action_attempt_to_string(self, input: ActionInput) -> str:
        return "Attempting to get phone number and query contact center"

    def action_result_to_string(self, input: ActionInput, output: ActionOutput) -> str:
        if output["result"]["success"]:
            return "Successfully retrieved contact information"
        else:
            return "Failed to retrieve contact information"


class GetPhoneAndQueryContactCenterAction(
    TwilioPhoneConversationAction[
        GetPhoneAndQueryContactCenterActionConfig, EmptyParameters, dict
    ]
):
    description: str = """Search for caller name, phone number, and email addresses."""

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
            quiet=False,
            is_interruptible=False,
            should_respond="always",
        )

    async def run(
        self, action_input: ActionInput[EmptyParameters]
    ) -> dict:
        twilio_call_sid = self.get_twilio_sid(action_input)
        logger.debug(f"Twilio Call SID: {twilio_call_sid}")

        twilio_client = self.conversation_state_manager.create_twilio_client()

        url = f"https://api.twilio.com/2010-04-01/Accounts/{twilio_client.get_telephony_config().account_sid}/Calls/{twilio_call_sid}.json"

        async with AsyncRequestor().get_session() as session:
            async with session.get(url, auth=twilio_client.auth) as response:
                if response.status != 200:
                    logger.error(f"Failed to get call details: {response.status} {response.reason}")
                    result = {"success": False}
                    agent_message = "Failed to retrieve call details."
                    return {
                        "result": result,
                        "agent_message": agent_message
                    }
                else:
                    call_details = await response.json()
                    logger.debug(f"Call Details: {call_details}")

        phone_number = call_details.get('from', '')
        logger.debug(f"Extracted Phone Number: {phone_number}")

        if not phone_number:
            logger.error("No phone number found in call details.")
            result = {"success": False}
            agent_message = "No phone number found in call details."
            return {
                "result": result,
                "agent_message": agent_message
            }

        server_url = os.environ.get("PORTAL_URL")
        headers = {
            'Content-Type': 'application/json',
            'X-Auth-Token': os.environ.get("PORTAL_AUTH_TOKEN"),
            'X-User-Id': os.environ.get("PORTAL_USER_ID"),
        }

        if not server_url or not headers['X-Auth-Token'] or not headers['X-User-Id']:
            logger.error("Missing environment variables for PORTAL_URL, PORTAL_AUTH_TOKEN, or PORTAL_USER_ID.")
            result = {"success": False}
            agent_message = "Configuration error: Missing environment variables."
            return {
                "result": result,
                "agent_message": agent_message
            }

        contact_info = await query_contact_center(server_url, headers, phone_number)
        logger.debug(f"Contact Info Retrieved: {contact_info}")

        # Determine success based on whether contact_info is not empty
        if contact_info.get("name") != "EMPTY":
            success = True
            email_addresses = contact_info.get('email_addresses')
            if isinstance(email_addresses, list):
                # Ensure all items are strings
                email_addresses_str = ', '.join([str(email) for email in email_addresses]) if email_addresses else "EMPTY"
            else:
                email_addresses_str = "EMPTY"
            # Structured message string for easy parsing by the agent
            agent_message = (
                f"I've successfully retrieved your contact information. "
                f"Your name is {contact_info.get('name')}, "
                f"your phone number is {contact_info.get('phone_number')}, "
                f"and your email address is {email_addresses_str}."
            )
            result = {"success": True}
        else:
            success = False
            agent_message = "Caller not found in contact center."
            result = {"success": False}

        logger.debug(f"Final Contact Info Message: {agent_message}")
        return {
            "result": result,
            "agent_message": agent_message
        }


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
                        # Extract email addresses, assuming 'visitorEmails' is a list of dicts
                        visitor_emails = contact.get('visitorEmails', [])
                        if isinstance(visitor_emails, list):
                            # Extract 'email' field from each dict if exists
                            email_addresses = [email.get('email') for email in visitor_emails if 'email' in email]
                        else:
                            email_addresses = []

                        contact_info = {
                            "name": contact.get('name', 'EMPTY') or "EMPTY",
                            "phone_number": normalized_phone,
                            "email_addresses": email_addresses if email_addresses else "EMPTY"
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
