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
from aiohttp import ClientSession  # Ensure this import is present

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



def normalize_phone_number(phone):
    """
    Normalize the phone number to ensure it is a 10-digit number by stripping '+1' or '1' prefixes.

    Parameters:
        phone (str): The input phone number.

    Returns:
        str: The normalized 10-digit phone number.

    Raises:
        ValueError: If the phone number format is invalid.
    """
    # Remove any non-digit characters
    phone = re.sub(r"[^\d]", "", phone)
    logger.debug(f"Raw Phone Number after removing non-digit characters: {phone}")

    # Case 1: Phone number starts with '1' and is 11 digits
    if re.fullmatch(r"1\d{10}", phone):
        normalized_phone = phone[1:]
        logger.debug(f"Phone number starts with '1'. Normalized to: {normalized_phone}")
        return normalized_phone

    # Case 2: Phone number is exactly 10 digits
    elif re.fullmatch(r"\d{10}", phone):
        logger.debug("Phone number is already in the correct 10-digit format.")
        return phone

    else:
        # Handle invalid formats as needed
        logger.error("Invalid phone number format.")
        raise ValueError("Invalid phone number format. Please provide a 10-digit number or 11-digit starting with '1'.")

async def add_to_contact_center(
    server_url, headers, phone, caller_name=None, email_address=None
):
    # Normalize phone number
    try:
        phone = normalize_phone_number(phone)
    except ValueError as ve:
        logger.error(f"Phone normalization error: {ve}")
        return False, str(ve)

    logger.debug(f"Normalized Phone Number: {phone}")

    params = {"phone": phone}

    try:
        async with ClientSession() as session:
            # Search for existing contact
            async with session.get(
                f"{server_url}/api/v1/omnichannel/contact.search",
                headers=headers,
                params=params,
            ) as r_search:
                if r_search.status != 200:
                    logger.error(
                        f"Failed to search contact: {r_search.status} {r_search.reason}"
                    )
                    cnt = {}
                else:
                    r_search_json = await r_search.json()
                    cnt = r_search_json.get("contact", {})
    except Exception as e:
        logger.error(f"Exception during contact search: {e}")
        cnt = {}

    if not cnt:
        # Create a random id and token
        token = secrets.token_urlsafe(22)
        _id = secrets.token_urlsafe(17)
        data = {
            "_id": _id,
            "token": token,
            "phone": phone,
            "name": caller_name,
            "email": email_address,
        }
        logger.debug(f"Data to send: {data}")
        try:
            async with ClientSession() as session:
                async with session.post(
                    f"{server_url}/api/v1/omnichannel/contact",
                    headers=headers,
                    data=json.dumps(data),
                ) as r_add:
                    if r_add.status != 200:
                        logger.error(
                            f"Failed to add contact: {r_add.status} {r_add.reason}"
                        )
                        return False, "Unable to add contact"
                    else:
                        logger.debug("Contact added successfully")
                        c_response = {"token": token, "_id": _id}
                        return True, c_response
        except Exception as e:
            logger.error(f"Exception during contact addition: {e}")
            return False, f"Exception occurred: {e}"
    else:
        logger.debug("Contact already exists, updating contact")
        contact_id = cnt.get("_id")
        if not contact_id:
            logger.error("Contact ID not found in contact search result")
            return False, "Contact ID not found"

        # Get token using /api/v1/livechat/visitors.info?visitorId=_id
        try:
            params = {'visitorId': contact_id}
            async with ClientSession() as session:
                async with session.get(
                    f"{server_url}/api/v1/livechat/visitors.info",
                    headers=headers,
                    params=params,
                ) as r_info:
                    if r_info.status != 200:
                        logger.error(
                            f"Failed to get visitor info: {r_info.status} {r_info.reason}"
                        )
                        return False, "Unable to get visitor info"
                    else:
                        r_info_json = await r_info.json()
                        token = r_info_json.get('visitor', {}).get('token')
                        if not token:
                            logger.error("Token not found in visitor info")
                            return False, "Token not found in visitor info"
        except Exception as e:
            logger.error(f"Exception during getting visitor info: {e}")
            return False, f"Exception occurred: {e}"

        # Build data with required fields
        data = {
            "_id": contact_id,
            "token": token,
            "name": caller_name,
            "phone": phone,
            "email": email_address,
        }

        logger.debug(f"Data to send for update: {data}")

        try:
            async with ClientSession() as session:
                async with session.post(
                    f"{server_url}/api/v1/omnichannel/contact",
                    headers=headers,
                    data=json.dumps(data),
                ) as r_update:
                    if r_update.status != 200:
                        logger.error(
                            f"Failed to update contact: {r_update.status} {r_update.reason}"
                        )
                        return False, "Unable to update contact"
                    else:
                        logger.debug("Contact updated successfully")
                        return True, {"message": "Contact updated successfully"}
        except Exception as e:
            logger.error(f"Exception during contact update: {e}")
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
