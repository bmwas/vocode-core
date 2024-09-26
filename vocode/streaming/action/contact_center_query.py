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


class QueryContactCenterResponse(BaseModel):
    success: bool
    result: Optional[dict]


class GetPhoneAndQueryContactCenterActionConfig(
    VocodeActionConfig, type="action_get_phone_and_query_contact_center"
):
    def action_attempt_to_string(self, input: ActionInput) -> str:
        return "Attempting to retrieve caller contact information from contact center"

    def action_result_to_string(self, input: ActionInput, output: ActionOutput) -> str:
        # Return the agent_message directly if available
        if output.response and output.response.result and "agent_message" in output.response.result:
            return output.response.result["agent_message"]
        elif output.response.success:
            return "Operation was successful, but no message provided."
        else:
            return "Failed to retrieve contact information"


class GetPhoneAndQueryContactCenterAction(
    TwilioPhoneConversationAction[
        GetPhoneAndQueryContactCenterActionConfig, EmptyParameters, QueryContactCenterResponse
    ]
):
    description: str = """
    Queries caller information such as; name, phone number, and email addresses. 
    Use this if faced with ANY of these scenarios.
    1) IF you've received a new call
    2) IF at any point you do not know the caller's personal information i.e. name, address and email address or if any of their personal information is marked as EMPTY
    3) IF a caller is wondering why you called them earlier or say they're returning your call
    Query user name, phone, address and email if this is a new call
    4) Preferrably to use this at the beginning of every call.
    """
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
            quiet=False,
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
                    success = False
                    agent_message = "Failed to get caller details"
                    message = {
                        "result": {"success": False},
                        "agent_message": agent_message} 
                    return ActionOutput(
                        action_type=action_input.action_config.type,
                        response=QueryContactCenterResponse(success=success, result=message),
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
                response=QueryContactCenterResponse(success=success, result=message),
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
            agent_message = "Caller query was unsuccessful"
            message = {
                "result": {"success": False},
                "agent_message": agent_message}            
            return ActionOutput(
                action_type=action_input.action_config.type,
                response=QueryContactCenterResponse(success=success, result=message),
            )

        contact_info = await query_contact_center(server_url, headers, phone_number)
        logger.debug(f"Contact Info Retrieved: {contact_info}")

        # Determine success based on whether contact_info is not empty
        if contact_info.get("name") != "EMPTY":
            success = True
            email_addresses = contact_info.get('email_addresses')
            # Structured message string for easy parsing by the agent
            agent_message = (
                f"Caller name is {contact_info.get('name')}, "
                f"phone number is {contact_info.get('phone_number')}, "
                f"and email address is {contact_info.get('email_addresses')}."
            )
            message = {
                "result": {"success": True},
                "agent_message": agent_message}
        else:
            success = False
            agent_message = "Caller query was unsuccessful"
            message = {
                "result": {"success": True},
                "agent_message": agent_message}
        logger.debug(f"Final Contact Info Message: {agent_message}")
        return ActionOutput(
            action_type=action_input.action_config.type,
            response=QueryContactCenterResponse(success=success, result=message),
        )

"""
Function to make a get query to contact center with phone number as an input
"""
async def query_contact_center(server_url, headers, phone, name=None, email_addresses=None):
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
            # Search for the contact
            async with session.get(
                f'{server_url}/api/v1/omnichannel/contact.search',
                headers=headers,
                params=params
            ) as r_search:
                if r_search.status != 200:
                    logger.error(f"Failed to search contact: {r_search.status} {r_search.reason}")
                    contact_exists = False
                else:
                    r_search_json = await r_search.json()
                    contact = r_search_json.get('contact', {})
                    if not contact:
                        # Contact does not exist
                        logger.info("Contact not found, proceeding to create a new contact.")
                        contact_exists = False
                    else:
                        # Contact exists
                        contact_exists = True
                        contact_id = contact.get('_id')
                        logger.info(f"Contact found with ID: {contact_id}")

            if not contact_exists:
                # Proceed to create a new contact
                contact_data = {
                    'phone': normalized_phone,
                }
                if name:
                    contact_data['name'] = name
                if email_addresses:
                    # Assume email_addresses is a list of email addresses
                    contact_data['visitorEmails'] = [{'address': email} for email in email_addresses]
                else:
                    contact_data['visitorEmails'] = []

                # Log the creation data
                logger.info("Creating new contact with the following data:")
                logger.info(contact_data)

                async with session.post(
                    f'{server_url}/api/v1/omnichannel/contact',
                    headers=headers,
                    json=contact_data
                ) as r_create:
                    if r_create.status != 200:
                        logger.error(f"Failed to create contact: {r_create.status} {r_create.reason}")
                        contact_info = {
                            "name": name or "EMPTY",
                            "phone_number": normalized_phone,
                            "email_addresses": ", ".join(email_addresses) if email_addresses else "EMPTY"
                        }
                    else:
                        r_create_json = await r_create.json()
                        contact_info = {
                            "name": r_create_json.get('contact', {}).get('name', 'EMPTY'),
                            "phone_number": normalized_phone,
                            "email_addresses": ", ".join(email_addresses) if email_addresses else "EMPTY"
                        }
            else:
                # Contact exists, proceed to update it
                update_data = {}
                if name:
                    update_data['name'] = name
                if email_addresses:
                    update_data['visitorEmails'] = [{'address': email} for email in email_addresses]
                if update_data:
                    # Log when the contact edit is called and the data being sent
                    logger.info(f"Updating contact {contact_id} with the following data:")
                    logger.info(update_data)
                    async with session.put(
                        f'{server_url}/api/v1/omnichannel/contact/{contact_id}',
                        headers=headers,
                        json=update_data
                    ) as r_update:
                        if r_update.status != 200:
                            logger.error(f"Failed to update contact: {r_update.status} {r_update.reason}")
                        else:
                            logger.info(f"Contact {contact_id} updated successfully.")
                else:
                    logger.info(f"No update data provided for contact {contact_id}. Skipping update.")

                # Retrieve the updated contact information
                async with session.get(
                    f'{server_url}/api/v1/omnichannel/contact',
                    headers=headers,
                    params={'contactId': contact_id}
                ) as r_get_contact:
                    if r_get_contact.status != 200:
                        logger.error(f"Failed to retrieve contact after update: {r_get_contact.status} {r_get_contact.reason}")
                        contact_info = {
                            "name": name or "EMPTY",
                            "phone_number": normalized_phone,
                            "email_addresses": ", ".join(email_addresses) if email_addresses else "EMPTY"
                        }
                    else:
                        content_type = r_get_contact.headers.get('Content-Type', '')
                        if 'application/json' not in content_type:
                            logger.error(f"Unexpected Content-Type: {content_type}")
                            contact_info = {
                                "name": name or "EMPTY",
                                "phone_number": normalized_phone,
                                "email_addresses": ", ".join(email_addresses) if email_addresses else "EMPTY"
                            }
                        else:
                            contact_json = await r_get_contact.json()
                            contact = contact_json.get('contact', {})
                            # Extract email addresses
                            visitor_emails = contact.get('visitorEmails', [])
                            if isinstance(visitor_emails, list):
                                email_addresses_list = [email.get('address') for email in visitor_emails if 'address' in email]
                                email_addresses_str = ", ".join(email_addresses_list)
                            else:
                                email_addresses_str = "EMPTY"
                            contact_info = {
                                "name": contact.get('name', 'EMPTY'),
                                "phone_number": normalized_phone,
                                "email_addresses": email_addresses_str if email_addresses_str else "EMPTY"
                            }
    except Exception as e:
        logger.error(f"Exception during contact search or update: {e}")
        contact_info = {
            "name": "EMPTY",
            "phone_number": "EMPTY",
            "email_addresses": "EMPTY"
        }

    logger.debug(f"Final Contact Info: {contact_info}")
    return contact_info