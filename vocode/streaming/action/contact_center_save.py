from typing import Literal, Optional, Type, Union, get_args

from loguru import logger
from pydantic.v1 import BaseModel, Field

from vocode.streaming.action.phone_call_action import (
    TwilioPhoneConversationAction,
    VonagePhoneConversationAction,
)
from vocode.streaming.models.actions import ActionConfig as VocodeActionConfig
from vocode.streaming.models.actions import ActionInput, ActionOutput
from vocode.streaming.utils.async_requester import AsyncRequestor
from vocode.streaming.utils.phone_numbers import sanitize_phone_number
from vocode.streaming.utils.state_manager import (
    TwilioPhoneConversationStateManager,
    VonagePhoneConversationStateManager,
)

import os
import json
import secrets


class AddToContactCenterEmptyParameters(BaseModel):
    pass


class AddToContactCenterRequiredParameters(BaseModel):
    caller_name: str = Field(..., description="The name of the caller")
    email_address: str = Field(..., description="The email address of the caller")


AddToContactCenterParameters = Union[
    AddToContactCenterEmptyParameters, AddToContactCenterRequiredParameters
]


class AddToContactCenterResponse(BaseModel):
    success: bool
    message: Optional[str] = None


class AddToContactCenterVocodeActionConfig(
    VocodeActionConfig, type="action_add_to_contact_center"
):  # type: ignore
    caller_name: Optional[str] = Field(
        None, description="The name of the caller"
    )
    email_address: Optional[str] = Field(
        None, description="The email address of the caller"
    )

    def get_caller_name(self, input: ActionInput) -> str:
        if isinstance(input.params, AddToContactCenterRequiredParameters):
            return input.params.caller_name
        elif isinstance(input.params, AddToContactCenterEmptyParameters):
            assert self.caller_name, "caller name must be set"
            return self.caller_name
        else:
            raise TypeError("Invalid input params type")

    def get_email_address(self, input: ActionInput) -> str:
        if isinstance(input.params, AddToContactCenterRequiredParameters):
            return input.params.email_address
        elif isinstance(input.params, AddToContactCenterEmptyParameters):
            assert self.email_address, "email address must be set"
            return self.email_address
        else:
            raise TypeError("Invalid input params type")

    def action_attempt_to_string(self, input: ActionInput) -> str:
        return f"Attempting to add contact to contact center"

    def action_result_to_string(
        self, input: ActionInput, output: ActionOutput
    ) -> str:
        assert isinstance(output.response, AddToContactCenterResponse)
        if output.response.success:
            action_description = "Successfully added contact to contact center"
        else:
            action_description = f"Failed to add contact to contact center - notify manager: {output.response.message}"
        return action_description


FUNCTION_DESCRIPTION = f"""Create, add, edit, update, save a caller's personal information (i.e. name, phone number and email address).
Use at any point during an ongoing call to create or edit caller's personal information in the contact center"""

QUIET = False
IS_INTERRUPTIBLE = False
SHOULD_RESPOND: Literal["always"] = "always"


class TwilioAddToContactCenter(
    TwilioPhoneConversationAction[
        AddToContactCenterVocodeActionConfig,
        AddToContactCenterParameters,
        AddToContactCenterResponse,
    ]
):
    description: str = FUNCTION_DESCRIPTION
    response_type: Type[AddToContactCenterResponse] = AddToContactCenterResponse
    conversation_state_manager: TwilioPhoneConversationStateManager

    @property
    def parameters_type(self) -> Type[AddToContactCenterParameters]:
        if self.action_config.caller_name and self.action_config.email_address:
            return AddToContactCenterEmptyParameters
        else:
            return AddToContactCenterRequiredParameters

    def __init__(
        self,
        action_config: AddToContactCenterVocodeActionConfig,
    ):
        super().__init__(
            action_config,
            quiet=QUIET,
            is_interruptible=IS_INTERRUPTIBLE,
            should_respond=SHOULD_RESPOND,
        )

    async def run(
        self, action_input: ActionInput[AddToContactCenterParameters]
    ) -> ActionOutput[AddToContactCenterResponse]:
        caller_name = self.action_config.get_caller_name(action_input)
        email_address = self.action_config.get_email_address(action_input)

        # Extract phone number from Twilio using twilio_call_sid
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
                    message = "Failed to get caller details"
                    return ActionOutput(
                        action_type=action_input.action_config.type,
                        response=AddToContactCenterResponse(
                            success=success, message=message
                        ),
                    )
                else:
                    call_details = await response.json()
                    logger.debug(f"Call Details: {call_details}")

        phone_number = call_details.get("from", "")
        logger.debug(f"Extracted Phone Number: {phone_number}")

        sanitized_phone_number = sanitize_phone_number(phone_number)

        server_url = os.environ.get("PORTAL_URL")
        headers = {
            "Content-Type": "application/json",
            "X-Auth-Token": os.environ.get("PORTAL_AUTH_TOKEN"),
            "X-User-Id": os.environ.get("PORTAL_USER_ID"),
        }
        success, response_message = await add_to_contact_center(
            server_url,
            headers,
            sanitized_phone_number,
            caller_name,
            email_address,
        )

        return ActionOutput(
            action_type=action_input.action_config.type,
            response=AddToContactCenterResponse(
                success=success, message=str(response_message)
            ),
        )

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
                    async with session.put(
                        f'{server_url}/api/v1/omnichannel/contact/{contact_id}',
                        headers=headers,
                        json=update_data
                    ) as r_update:
                        if r_update.status != 200:
                            logger.error(f"Failed to update contact: {r_update.status} {r_update.reason}")
                        else:
                            logger.info(f"Contact {contact_id} updated successfully.")
                # Retrieve the updated contact information
                async with session.get(
                    f'{server_url}/api/v1/omnichannel/contact/{contact_id}',
                    headers=headers
                ) as r_get_contact:
                    if r_get_contact.status != 200:
                        logger.error(f"Failed to retrieve contact after update: {r_get_contact.status} {r_get_contact.reason}")
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