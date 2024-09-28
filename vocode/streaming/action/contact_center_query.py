import re
import json
import os
from typing import Type, Optional, Union, Literal

from loguru import logger
from pydantic.v1 import BaseModel, Field

from vocode.streaming.action.phone_call_action import TwilioPhoneConversationAction
from vocode.streaming.models.actions import (
    ActionConfig as VocodeActionConfig,
    ActionInput,
    ActionOutput,
)
from vocode.streaming.utils.async_requester import AsyncRequestor
from vocode.streaming.utils.state_manager import TwilioPhoneConversationStateManager
from vocode.streaming.models.actions import FunctionCallActionTrigger

import aiohttp


# ---------------------------
# Pydantic Models
# ---------------------------

class EmptyParameters(BaseModel):
    """Represents cases where no additional parameters are provided."""
    pass


class QueryContactCenterResponse(BaseModel):
    """Represents the response from querying the contact center."""
    success: bool
    result: Optional[dict]


# ---------------------------
# Action Configuration
# ---------------------------

class GetPhoneAndQueryContactCenterActionConfig(
    VocodeActionConfig, type="action_get_phone_and_query_contact_center"
):
    """
    Configuration for the GetPhoneAndQueryContactCenterAction.

    Attributes:
        direction (Literal["to", "from"]): Direction of the call to extract the phone number.
    """
    direction: Literal["to", "from"] = Field(
        ..., description="Direction of the call: 'to' or 'from'"
    )

    def action_attempt_to_string(self, input: ActionInput) -> str:
        return "Attempting to retrieve caller contact information from contact center"

    def action_result_to_string(
        self, input: ActionInput, output: ActionOutput
    ) -> str:
        # Return the agent_message directly if available
        if (
            output.response
            and output.response.result
            and "agent_message" in output.response.result
        ):
            return output.response.result["agent_message"]
        elif output.response.success:
            return "Operation was successful, but no message provided."
        else:
            return "Failed to retrieve contact information"


# ---------------------------
# Action Implementation
# ---------------------------

class GetPhoneAndQueryContactCenterAction(
    TwilioPhoneConversationAction[
        GetPhoneAndQueryContactCenterActionConfig, EmptyParameters, QueryContactCenterResponse
    ]
):
    """
    Action to retrieve caller contact information from the contact center.

    This action queries caller information such as name, phone number, and email addresses.
    """

    description: str = """
    Queries caller information such as; name, phone number, and email addresses. 
    Use this if faced with ANY of these scenarios.
    1) IF you've received a new call
    2) IF at any point you do not know the caller's personal information i.e. name, address and email address or if any of their personal information is marked as EMPTY
    3) IF a caller is wondering why you called them earlier or say they're returning your call
    Query user name, phone, address and email if this is a new call
    4) Preferably to use this at the beginning of every call.
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
        """
        Initializes the action with the given configuration.

        Args:
            action_config (GetPhoneAndQueryContactCenterActionConfig): The configuration for the action.
        """
        super().__init__(
            action_config,
            quiet=False,
            is_interruptible=False,
            should_respond="always",
        )

    async def run(
        self, action_input: ActionInput[EmptyParameters]
    ) -> ActionOutput[QueryContactCenterResponse]:
        """
        Executes the action to retrieve caller contact information.

        Args:
            action_input (ActionInput[EmptyParameters]): The input for the action.

        Returns:
            ActionOutput[QueryContactCenterResponse]: The result of the action.
        """
        # Access the direction from the action configuration
        direction = self.action_config.direction
        logger.debug(f"Call direction: {direction}")

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
                    agent_message = "Failed to get caller details"
                    message = {
                        "result": {"success": False},
                        "agent_message": agent_message,
                    }
                    return ActionOutput(
                        action_type=action_input.action_config.type,
                        response=QueryContactCenterResponse(
                            success=success, result=message
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
            agent_message = "No phone number found in call details"
            message = {
                "result": {"success": False},
                "agent_message": agent_message,
            }

            return ActionOutput(
                action_type=action_input.action_config.type,
                response=QueryContactCenterResponse(success=success, result=message),
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
            agent_message = "Caller query was unsuccessful"
            message = {
                "result": {"success": False},
                "agent_message": agent_message,
            }
            return ActionOutput(
                action_type=action_input.action_config.type,
                response=QueryContactCenterResponse(success=success, result=message),
            )

        contact_info = await query_contact_center(server_url, headers, phone_number)
        logger.debug(f"Contact Info Retrieved: {contact_info}")

        # Determine success based on whether contact_info is not empty
        if contact_info.get("name") != "EMPTY":
            success = True
            email_addresses = contact_info.get("email_addresses")
            # Structured message string for easy parsing by the agent
            agent_message = (
                f"Caller name is {contact_info.get('name')}, "
                f"phone number is {contact_info.get('phone_number')}, "
                f"and email address is {contact_info.get('email_addresses')}."
            )
            message = {
                "result": {"success": True},
                "agent_message": agent_message,
            }
        else:
            success = False
            agent_message = "Caller query was unsuccessful"
            message = {
                "result": {"success": False},
                "agent_message": agent_message,
            }
        logger.debug(f"Final Contact Info Message: {agent_message}")
        return ActionOutput(
            action_type=action_input.action_config.type,
            response=QueryContactCenterResponse(
                success=success, result=message
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