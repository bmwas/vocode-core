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
import re


import aiohttp
from aiohttp import ClientSession
import phonenumbers


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


FUNCTION_DESCRIPTION = f"""Used in the following scenarios:
1) Create or add a new caller's contact or personal information (i.e. name, address and email address)
2) Update or edit an existing caller's personal information i.e. when a caller corrects their personal information for example, if email on file was abc@gmail.com but caller corrected it to jhn@gmail.com
3) Used at any point in an ongoing call to create a new contact or edit/update an existing contact with new information.
4) Create or updated information in system or contact center
"""

QUIET = False
IS_INTERRUPTIBLE = False
SHOULD_RESPOND: Literal["always"] = "always"





def normalize_phone_number(phone_number):
    """
    Normalize the phone number to ensure it is a 10-digit number by removing the country code.
    
    Parameters:
        phone_number (str): The input phone number.
    
    Returns:
        str: The normalized 10-digit phone number, or None if invalid.
    """
    logger.debug(f"normalize_phone_number received: {phone_number}")
    if not phone_number:
        logger.error("No phone number provided to normalize.")
        return None
    try:
        # Parse the phone number
        parsed_number = phonenumbers.parse(phone_number, None)
        
        # Check if the number is valid
        if not phonenumbers.is_valid_number(parsed_number):
            logger.error("Invalid phone number format.")
            return None
        
        # Get the national number (without country code)
        national_number = str(parsed_number.national_number)
        logger.debug(f"Normalized Phone Number: {national_number}")
        return national_number
    except phonenumbers.phonenumberutil.NumberParseException:
        # Return None if the number cannot be parsed
        logger.error("NumberParseException: Unable to parse phone number.")
        return None

async def add_to_contact_center(
    session: ClientSession,
    server_url: str,
    headers: dict,
    phone: str,
    caller_name: str = None,
    email_address: str = None
) -> (bool, dict or str): # type: ignore
    """
    Adds or updates a contact in the contact center.

    Parameters:
        session (ClientSession): The aiohttp client session.
        server_url (str): The base URL of the contact center server.
        headers (dict): HTTP headers for the requests.
        phone (str): The phone number of the contact.
        caller_name (str, optional): The name of the caller.
        email_address (str, optional): The email address of the contact.

    Returns:
        tuple: (bool, dict or str) indicating success status and response or error message.
    """
    # Validate that session is a ClientSession instance
    if not isinstance(session, ClientSession):
        logger.error(f"Invalid session object: {session} (type: {type(session)})")
        return False, "Invalid session object provided."

    logger.debug(f"Starting add_to_contact_center with phone: {phone}")

    # Step 1: Search for existing contact by phone number (original phone number)
    params_search = {"phone": phone}
    try:
        async with session.get(
            f"{server_url}/api/v1/omnichannel/contact.search",
            headers=headers,
            params=params_search,
            timeout=aiohttp.ClientTimeout(total=10)  # Set a timeout for the request
        ) as response_search:
            logger.debug(f"Search Contact Response Status Code: {response_search.status} {response_search.reason}")
            response_text = await response_search.text()
            logger.debug(f"Search Contact Response Body: {response_text}")

            if 200 <= response_search.status < 300:
                try:
                    search_json = await response_search.json()
                    search_result = search_json.get("contact", {})
                    if search_result:
                        logger.debug(f"Contact found: {search_result}")
                    else:
                        logger.debug("No contact found with the given phone number.")
                except json.JSONDecodeError:
                    logger.error("Failed to decode JSON from search contact response.")
                    search_result = {}
            else:
                logger.error(
                    f"Failed to search contact: {response_search.status} {response_search.reason}"
                )
                search_result = {}
    except aiohttp.ClientError as e:
        logger.error(f"Exception during contact search: {e}")
        search_result = {}

    if search_result:
        # Step 2: Update existing contact
        logger.debug("Proceeding to update the existing contact.")

        contact_id = search_result.get("_id")
        token = search_result.get("token")

        if not contact_id:
            logger.error("Contact ID not found in search result.")
            return False, "Contact ID not found in search result."

        if not token:
            logger.error("Token not found in search result.")
            return False, "Token not found in search result."

        # Normalize phone number for update
        normalized_phone = normalize_phone_number(phone)
        if not normalized_phone:
            logger.error("Failed to normalize phone number for update.")
            return False, "Failed to normalize phone number for update."

        # Prepare the data payload for updating
        data_update = {
            "_id": contact_id,
            "token": token,
            "name": caller_name,
            "phone": normalized_phone,  # Using normalized phone number
            "email": email_address,
        }

        logger.debug(f"Data to send for updating contact: {data_update}")

        try:
            async with session.post(
                f"{server_url}/api/v1/omnichannel/contact",
                headers=headers,
                data=json.dumps(data_update),
                timeout=aiohttp.ClientTimeout(total=10)  # Set a timeout for the request
            ) as response_update:
                logger.debug(f"Update Contact Response Status Code: {response_update.status} {response_update.reason}")
                response_update_text = await response_update.text()
                logger.debug(f"Update Contact Response Body: {response_update_text}")

                if 200 <= response_update.status < 300:
                    try:
                        response_json = await response_update.json()
                        logger.debug("Contact updated successfully.")
                        return True, response_json
                    except json.JSONDecodeError:
                        logger.error("Failed to decode JSON from update contact response.")
                        return False, "Contact updated, but failed to parse response."
                else:
                    logger.error(
                        f"Failed to update contact: {response_update.status} {response_update.reason}"
                    )
                    return False, "Unable to update contact"
        except aiohttp.ClientError as e:
            logger.error(f"Exception during contact update: {e}")
            return False, f"Exception occurred: {e}"
    else:
        # Step 3: Create new contact
        logger.debug("Proceeding to create a new contact.")

        # Normalize phone number for creation
        normalized_phone = normalize_phone_number(phone)
        if not normalized_phone:
            logger.error("Failed to normalize phone number for creation.")
            return False, "Failed to normalize phone number for creation."

        # Generate a random _id and token
        _id = secrets.token_urlsafe(17)
        token = secrets.token_urlsafe(22)
        data_create = {
            "_id": _id,
            "token": token,
            "phone": normalized_phone,
            "name": caller_name,
            "email": email_address,
        }

        logger.debug(f"Data to send for creating contact: {data_create}")

        try:
            async with session.post(
                f"{server_url}/api/v1/omnichannel/contact",
                headers=headers,
                data=json.dumps(data_create),
                timeout=aiohttp.ClientTimeout(total=10)  # Set a timeout for the request
            ) as response_create:
                logger.debug(f"Create Contact Response Status Code: {response_create.status} {response_create.reason}")
                response_create_text = await response_create.text()
                logger.debug(f"Create Contact Response Body: {response_create_text}")

                if 200 <= response_create.status < 300:
                    try:
                        response_json = await response_create.json()
                        logger.debug("Contact added successfully.")
                        return True, response_json
                    except json.JSONDecodeError:
                        logger.error("Failed to decode JSON from create contact response.")
                        return False, "Contact created, but failed to parse response."
                else:
                    logger.error(
                        f"Failed to add contact: {response_create.status} {response_create.reason}"
                    )
                    return False, "Unable to add contact"
        except aiohttp.ClientError as e:
            logger.error(f"Exception during contact addition: {e}")
            return False, f"Exception occurred: {e}"



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
