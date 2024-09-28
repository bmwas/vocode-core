from typing import Literal, Optional, Type, Union

from loguru import logger
from pydantic.v1 import BaseModel, Field

from vocode.streaming.action.phone_call_action import TwilioPhoneConversationAction
from vocode.streaming.models.actions import (
    ActionConfig as VocodeActionConfig,
    ActionInput,
    ActionOutput,
)
from vocode.streaming.models.actions import FunctionCallActionTrigger
from vocode.streaming.utils.async_requester import AsyncRequestor
from vocode.streaming.utils.state_manager import TwilioPhoneConversationStateManager

import os
import json
import secrets
import phonenumbers


# ---------------------------
# Pydantic Models
# ---------------------------

class AddToContactCenterEmptyParameters(BaseModel):
    """Represents cases where no additional parameters are provided."""
    pass


class AddToContactCenterRequiredParameters(BaseModel):
    """Represents required parameters for adding to the contact center."""
    caller_name: str = Field(..., description="The name of the caller")
    email_address: str = Field(..., description="The email address of the caller")


AddToContactCenterParameters = Union[
    AddToContactCenterEmptyParameters, AddToContactCenterRequiredParameters
]


class AddToContactCenterResponse(BaseModel):
    """Represents the response after attempting to add to the contact center."""
    success: bool
    message: Optional[str] = None


# ---------------------------
# Action Configuration
# ---------------------------

class AddToContactCenterVocodeActionConfig(
    VocodeActionConfig, type="action_add_to_contact_center"
):
    """
    Configuration for the AddToContactCenterAction.

    Attributes:
        direction (Literal["to", "from"]): Direction of the call to extract the phone number.
        caller_name (Optional[str]): The name of the caller (optional if provided in parameters).
        email_address (Optional[str]): The email address of the caller (optional if provided in parameters).
    """
    direction: Literal["to", "from"] = Field(
        ..., description="Direction of the call: 'to' or 'from'"
    )
    caller_name: Optional[str] = Field(
        None, description="The name of the caller"
    )
    email_address: Optional[str] = Field(
        None, description="The email address of the caller"
    )

    def get_caller_name(self, input: ActionInput) -> str:
        """
        Retrieves the caller name from the action input parameters or configuration.

        Args:
            input (ActionInput): The input to the action.

        Returns:
            str: The caller's name.

        Raises:
            TypeError: If the input parameters are of an invalid type.
            AssertionError: If the caller name is not set in the configuration when parameters are empty.
        """
        if isinstance(input.params, AddToContactCenterRequiredParameters):
            return input.params.caller_name
        elif isinstance(input.params, AddToContactCenterEmptyParameters):
            assert self.caller_name, "caller name must be set"
            return self.caller_name
        else:
            raise TypeError("Invalid input params type")

    def get_email_address(self, input: ActionInput) -> str:
        """
        Retrieves the email address from the action input parameters or configuration.

        Args:
            input (ActionInput): The input to the action.

        Returns:
            str: The caller's email address.

        Raises:
            TypeError: If the input parameters are of an invalid type.
            AssertionError: If the email address is not set in the configuration when parameters are empty.
        """
        if isinstance(input.params, AddToContactCenterRequiredParameters):
            return input.params.email_address
        elif isinstance(input.params, AddToContactCenterEmptyParameters):
            assert self.email_address, "email address must be set"
            return self.email_address
        else:
            raise TypeError("Invalid input params type")

    def action_attempt_to_string(self, input: ActionInput) -> str:
        """
        Returns a string representation of the action attempt.

        Args:
            input (ActionInput): The input to the action.

        Returns:
            str: Description of the action attempt.
        """
        return "Attempting to add contact to contact center"

    def action_result_to_string(
        self, input: ActionInput, output: ActionOutput
    ) -> str:
        """
        Returns a string representation of the action result.

        Args:
            input (ActionInput): The input to the action.
            output (ActionOutput): The output from the action.

        Returns:
            str: Description of the action result.
        """
        assert isinstance(output.response, AddToContactCenterResponse)
        if output.response.success:
            action_description = "Successfully added contact to contact center"
        else:
            action_description = f"Failed to add contact to contact center - notify manager: {output.response.message}"
        return action_description


FUNCTION_DESCRIPTION = """Used in the following scenarios:
1) Create or add a new caller's contact or personal information (i.e. name, address and email address)
2) Update or edit an existing caller's personal information i.e. when a caller corrects their personal information for example, if email on file was abc@gmail.com but caller corrected it to jhn@gmail.com
3) Used at any point in an ongoing call to create a new contact or edit/update an existing contact with new information.
4) Create or updated information in system or contact center
"""

QUIET = False
IS_INTERRUPTIBLE = False
SHOULD_RESPOND: Literal["always"] = "always"


# ---------------------------
# Helper Functions
# ---------------------------

def normalize_phone_number(phone_number: str) -> Optional[str]:
    """
    Normalize the phone number to ensure it is a 10-digit number by removing the country code.

    Parameters:
        phone_number (str): The input phone number.

    Returns:
        Optional[str]: The normalized 10-digit phone number, or None if invalid.
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
    server_url: str,
    headers: dict,
    phone: str,
    caller_name: str,
    email_address: str,
) -> tuple:
    """
    Adds a contact to the contact center. If the contact exists, it updates the contact.

    Args:
        server_url (str): The base URL of the contact center API.
        headers (dict): HTTP headers for the API requests.
        phone (str): The phone number of the contact.
        caller_name (str): The name of the caller.
        email_address (str): The email address of the contact.

    Returns:
        tuple: A tuple containing a boolean status and a response message or data.
    """
    params = {"phone": phone}

    try:
        async with AsyncRequestor().get_session() as session:
            # Step 1: Search for existing contact
            async with session.get(
                f"{server_url}/api/v1/omnichannel/contact.search",
                headers=headers,
                params=params,
            ) as r_search:
                if r_search.status != 200:
                    logger.error(
                        f"Failed to search contact: {r_search.status} {r_search.reason}"
                    )
                    cnt = []
                else:
                    r_search_json = await r_search.json()
                    cnt = r_search_json.get("contact", [])
    except Exception as e:
        logger.error(f"Exception during contact search: {e}")
        cnt = []

    # Step 2: If contact doesn't exist, create it
    if not cnt:
        # Generate random _id and token
        token = secrets.token_urlsafe(22)
        _id = secrets.token_urlsafe(17)
        normalized_phone = normalize_phone_number(phone)
        if not normalized_phone:
            return False, "Invalid phone number provided"

        data = {
            "_id": _id,
            "token": token,
            "phone": normalized_phone,
            "name": caller_name,
            "email": email_address,
        }
        logger.debug(f"Data to create: {data}")

        try:
            async with AsyncRequestor().get_session() as session:
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

    # Step 3: If contact exists, update it
    else:
        logger.debug("Contact already exists. Proceeding to update.")
        try:
            # Assuming 'cnt' is a list of contacts; take the first one
            contact = cnt[0] if isinstance(cnt, list) else cnt
            _id = contact.get("_id")
            token = contact.get("token")

            if not _id or not token:
                logger.error("Existing contact missing _id or token")
                return False, "Existing contact missing _id or token"

            # Normalize the phone number before updating
            normalized_phone = normalize_phone_number(phone)
            if not normalized_phone:
                return False, "Invalid phone number provided"

            update_data = {
                "_id": _id,
                "token": token,
                "phone": normalized_phone,
                "name": caller_name,
                "email": email_address,
            }
            logger.debug(f"Data to update: {update_data}")

            async with AsyncRequestor().get_session() as session:
                async with session.post(
                    f"{server_url}/api/v1/omnichannel/contact",
                    headers=headers,
                    data=json.dumps(update_data),
                ) as r_update:
                    if r_update.status != 200:
                        logger.error(
                            f"Failed to update contact: {r_update.status} {r_update.reason}"
                        )
                        return False, "Unable to update contact"
                    else:
                        logger.debug("Contact updated successfully")
                        return True, {"_id": _id, "token": token}
        except Exception as e:
            logger.error(f"Exception during contact update: {e}")
            return False, f"Exception occurred: {e}"


# ---------------------------
# Action Implementation
# ---------------------------

class AddToContactCenter(
    TwilioPhoneConversationAction[
        AddToContactCenterVocodeActionConfig,
        AddToContactCenterParameters,
        AddToContactCenterResponse,
    ]
):
    """
    Action to add or update caller details in the contact center.

    This action uses the Twilio call SID to retrieve call details, extract the phone number
    based on the specified direction, and add or update the contact information in the contact center.
    """
    description: str = FUNCTION_DESCRIPTION
    response_type: Type[AddToContactCenterResponse] = AddToContactCenterResponse
    conversation_state_manager: TwilioPhoneConversationStateManager

    @property
    def parameters_type(self) -> Type[AddToContactCenterParameters]:
        """
        Determines the type of parameters the action expects.

        Returns:
            Type[AddToContactCenterParameters]: The parameter type.
        """
        if self.action_config.caller_name and self.action_config.email_address:
            return AddToContactCenterEmptyParameters
        else:
            return AddToContactCenterRequiredParameters

    def __init__(
        self,
        action_config: AddToContactCenterVocodeActionConfig,
    ):
        """
        Initializes the action with the given configuration.

        Args:
            action_config (AddToContactCenterVocodeActionConfig): The configuration for the action.
        """
        super().__init__(
            action_config,
            quiet=QUIET,
            is_interruptible=IS_INTERRUPTIBLE,
            should_respond=SHOULD_RESPOND,
        )

    async def run(
        self, action_input: ActionInput[AddToContactCenterParameters]
    ) -> ActionOutput[AddToContactCenterResponse]:
        """
        Executes the action to add or update contact information in the contact center.

        Args:
            action_input (ActionInput[AddToContactCenterParameters]): The input for the action.

        Returns:
            ActionOutput[AddToContactCenterResponse]: The result of the action.
        """
        caller_name = self.action_config.get_caller_name(action_input)
        email_address = self.action_config.get_email_address(action_input)

        # Retrieve direction from the action configuration
        direction = self.action_config.direction
        logger.debug(f"Call direction from config: {direction}")

        # Extract phone number from Twilio using twilio_call_sid
        twilio_call_sid = self.get_twilio_sid(action_input)
        logger.debug(f"Twilio Call SID: {twilio_call_sid}")

        twilio_client = self.conversation_state_manager.create_twilio_client()

        # Construct the URL to fetch call details from Twilio
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

        # Use the direction parameter to extract the correct phone number
        phone_number = call_details.get(direction, "")
        logger.debug(f"Extracted Phone Number: {phone_number}")

        if not phone_number:
            logger.error("No phone number found in call details.")
            success = False
            message = "No phone number found in call details"
            return ActionOutput(
                action_type=action_input.action_config.type,
                response=AddToContactCenterResponse(
                    success=success, message=message
                ),
            )

        server_url = os.environ.get("PORTAL_URL")
        headers = {
            "Content-Type": "application/json",
            "X-Auth-Token": os.environ.get("PORTAL_AUTH_TOKEN"),
            "X-User-Id": os.environ.get("PORTAL_USER_ID"),
        }

        if not server_url or not headers["X-Auth-Token"] or not headers["X-User-Id"]:
            logger.error(
                "Missing environment variables for PORTAL_URL, PORTAL_AUTH_TOKEN, or PORTAL_USER_ID."
            )
            success = False
            message = "Caller query was unsuccessful"
            return ActionOutput(
                action_type=action_input.action_config.type,
                response=AddToContactCenterResponse(
                    success=success, message=message
                ),
            )

        success, response_message = await add_to_contact_center(
            server_url,
            headers,
            phone_number,
            caller_name,
            email_address,
        )

        return ActionOutput(
            action_type=action_input.action_config.type,
            response=AddToContactCenterResponse(
                success=success, message=str(response_message)
            ),
        )


# ---------------------------
# Helper Functions
# ---------------------------

async def query_contact_center(server_url: str, headers: dict, phone: str) -> dict:
    """
    Makes a GET request to the contact center API to retrieve contact information.

    Args:
        server_url (str): The base URL of the contact center API.
        headers (dict): HTTP headers for the API request.
        phone (str): The phone number to query.

    Returns:
        dict: Contact information retrieved from the contact center.
    """
    # Normalize the phone number
    if phone.startswith("+"):
        normalized_phone = phone
    elif phone.startswith("1"):
        normalized_phone = "+" + phone
    else:
        normalized_phone = "+1" + phone

    logger.debug(f"Normalized Phone Number: {normalized_phone}")

    params = {"phone": normalized_phone}

    try:
        async with AsyncRequestor().get_session() as session:
            async with session.get(
                f"{server_url}/api/v1/omnichannel/contact.search",
                headers=headers,
                params=params,
            ) as r_search:
                if r_search.status != 200:
                    logger.error(
                        f"Failed to search contact: {r_search.status} {r_search.reason}"
                    )
                    contact_info = {
                        "name": "EMPTY",
                        "phone_number": "EMPTY",
                        "email_addresses": "EMPTY",
                    }
                else:
                    r_search_json = await r_search.json()
                    contact = r_search_json.get("contact", {})

                    if not contact:
                        logger.error("Contact not found")
                        contact_info = {
                            "name": "EMPTY",
                            "phone_number": "EMPTY",
                            "email_addresses": "EMPTY",
                        }
                    else:
                        # Extract email addresses, using 'address' as the key
                        visitor_emails = contact.get("visitorEmails", [])
                        if isinstance(visitor_emails, list):
                            # Extract 'address' field from each dict if exists
                            email_addresses_list = [
                                email.get("address")
                                for email in visitor_emails
                                if "address" in email
                            ]
                            # Convert the list to a comma-separated string
                            email_addresses = ", ".join(email_addresses_list)
                        else:
                            email_addresses = ""

                        contact_info = {
                            "name": contact.get("name", "EMPTY") or "EMPTY",
                            "phone_number": normalized_phone,
                            "email_addresses": email_addresses
                            if email_addresses
                            else "EMPTY",
                        }
    except Exception as e:
        logger.error(f"Exception during contact search: {e}")
        contact_info = {
            "name": "EMPTY",
            "phone_number": "EMPTY",
            "email_addresses": "EMPTY",
        }
    logger.debug(f"Final Contact Info: {contact_info}")
    return contact_info