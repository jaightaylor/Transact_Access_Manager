#!/usr/bin/env python3
"""Transact API client with OAuth 1.0 HMAC-SHA1 authentication.

Handles the three-step OAuth flow (initiate → token → verify) and provides
methods for door access plan and board/meal plan management.
"""

import json as _json
from urllib.parse import parse_qs

import requests
from oauthlib.oauth1 import Client, SIGNATURE_HMAC_SHA1


class TransactOAuthClient:
    """OAuth 1.0 client for the Transact / BBTS management API."""

    def __init__(self, consumer_key, consumer_secret, hostname,
                 route_scheme, route_value):
        self.consumer_key = consumer_key
        self.consumer_secret = consumer_secret
        self.base_url = f"https://{hostname}"
        self.route_scheme = route_scheme
        self.route_value = route_value

        # Set after authenticate()
        self._token = None
        self._token_secret = None
        self._oauth_client = None

    # ── Properties ──────────────────────────────────────────────────────

    @property
    def is_authenticated(self):
        return self._oauth_client is not None

    # ── OAuth 1.0 three-step flow ───────────────────────────────────────

    def authenticate(self):
        """Run the full initiate → token → verify flow.

        Returns (True, "") on success or (False, error_message) on failure.
        """
        try:
            temp_token, temp_secret = self._initiate()
            token, token_secret = self._get_token(temp_token, temp_secret)
            self._verify(token, token_secret)
            self._token = token
            self._token_secret = token_secret
            self._oauth_client = Client(
                self.consumer_key,
                client_secret=self.consumer_secret,
                resource_owner_key=token,
                resource_owner_secret=token_secret,
                signature_method=SIGNATURE_HMAC_SHA1,
            )
            return True, ""
        except Exception as e:
            self._oauth_client = None
            return False, str(e)

    def reauthenticate(self):
        """Re-run the full auth flow to get fresh tokens.

        Call this before a batch of writes to avoid token expiry.
        Returns (True, "") on success or (False, error_message) on failure.
        """
        return self.authenticate()

    def _initiate(self):
        """Step 1: Request temporary OAuth token."""
        url = f"{self.base_url}/transact/api/initiate"
        client = Client(
            self.consumer_key,
            client_secret=self.consumer_secret,
            signature_method=SIGNATURE_HMAC_SHA1,
        )
        uri, headers, _ = client.sign(url, http_method="POST")
        resp = requests.post(uri, headers=headers, timeout=30)
        resp.raise_for_status()
        params = parse_qs(resp.text)
        return params["oauth_token"][0], params["oauth_token_secret"][0]

    def _get_token(self, temp_token, temp_secret):
        """Step 2: Exchange temporary token for access token."""
        url = f"{self.base_url}/transact/api/token"
        client = Client(
            self.consumer_key,
            client_secret=self.consumer_secret,
            resource_owner_key=temp_token,
            resource_owner_secret=temp_secret,
            signature_method=SIGNATURE_HMAC_SHA1,
        )
        uri, headers, _ = client.sign(url, http_method="POST")
        resp = requests.post(uri, headers=headers, timeout=30)
        resp.raise_for_status()
        params = parse_qs(resp.text)
        return params["oauth_token"][0], params["oauth_token_secret"][0]

    def _verify(self, token, token_secret):
        """Step 3: Validate the access token."""
        url = f"{self.base_url}/transact/api/verify"
        client = Client(
            self.consumer_key,
            client_secret=self.consumer_secret,
            resource_owner_key=token,
            resource_owner_secret=token_secret,
            signature_method=SIGNATURE_HMAC_SHA1,
        )
        uri, headers, _ = client.sign(url, http_method="POST")
        resp = requests.post(uri, headers=headers, timeout=30)
        resp.raise_for_status()

    # ── Signed request helpers ──────────────────────────────────────────

    def _signed_headers(self, url, method="GET"):
        """Build OAuth-signed headers.

        IMPORTANT: OAuth 1.0 signature base string must NOT include the
        request body for JSON payloads — only application/x-www-form-urlencoded
        bodies are included in the signature.  We therefore never pass the
        body to client.sign().
        """
        uri, headers, _ = self._oauth_client.sign(url, http_method=method)
        headers["Accept"] = "application/json"
        return uri, headers

    def _bbts_request(self, method, path, body_dict=None):
        """Execute a signed request against a BBTS management endpoint.

        Handles OAuth signing, institution-route header, JSON serialization,
        and returns (response, error_string).
        """
        url = f"{self.base_url}{path}"
        uri, headers = self._signed_headers(url, method=method)
        headers["TransactSP-Institution-Route"] = (
            f"{self.route_scheme} {self.route_value}"
        )

        kwargs = {"headers": headers, "timeout": 30}
        if body_dict is not None:
            headers["Content-Type"] = "application/json"
            kwargs["data"] = _json.dumps(body_dict)

        resp = requests.request(method, uri, **kwargs)
        return resp

    def _transact_request(self, method, path):
        """Execute a signed request against a /transact/api/ endpoint."""
        url = f"{self.base_url}{path}"
        uri, headers = self._signed_headers(url, method=method)
        return requests.request(method, uri, headers=headers, timeout=30)

    # ── Plan list retrieval ─────────────────────────────────────────────

    def get_door_access_plans(self):
        """Return list of all door access plans.

        Each plan is a dict with at least 'id' and 'active' keys.
        Returns (plans_list, error_message).
        """
        try:
            resp = self._bbts_request(
                "GET", "/BBTS/api/management/v1/doorAccessPlans")
            resp.raise_for_status()
            data = resp.json()
            return data.get("doorAccessPlans", []), ""
        except Exception as e:
            return [], str(e)

    def get_board_plans(self):
        """Return list of all board/meal plans.

        Returns (plans_list, error_message).
        """
        try:
            resp = self._bbts_request(
                "GET", "/bbts/api/management/v1/boardPlans")
            resp.raise_for_status()
            data = resp.json()
            plans = data.get("BoardPlans") or data.get("boardPlans") or []
            return plans, ""
        except Exception as e:
            return [], str(e)

    # ── Customer lookup ─────────────────────────────────────────────────

    def get_customer(self, customer_number):
        """Look up a customer by CustomerNumber (= AD employeeID).

        Returns (customer_dict, error_message). customer_dict is None if
        not found.
        """
        try:
            resp = self._transact_request(
                "GET",
                f"/transact/api/customer"
                f"?customerNumber={customer_number}&isActive=true")
            resp.raise_for_status()
            data = resp.json()
            customers = data.get("Customers") or data.get("customers") or []
            if customers:
                return customers[0], ""
            return None, "Customer not found in Transact"
        except Exception as e:
            return None, str(e)

    # ── Door access plan operations ─────────────────────────────────────

    def get_customer_door_plans(self, customer_number):
        """Get door access plans currently assigned to a customer."""
        try:
            resp = self._bbts_request(
                "GET",
                f"/bbts/api/management/v1"
                f"/customers/{customer_number}/doorAccessPlans")
            resp.raise_for_status()
            data = resp.json()
            return data.get("doorAccessPlans", []), ""
        except Exception as e:
            return [], str(e)

    def add_customer_door_plan(self, customer_number, plan_id):
        """Assign a door access plan to a customer.

        Returns (success, error_code, message).
        """
        try:
            resp = self._bbts_request(
                "POST",
                f"/bbts/api/management/v1"
                f"/customers/{customer_number}/doorAccessPlans",
                body_dict={"DoorAccessPlan": {"Id": plan_id}})
            return self._parse_operation_result(resp)
        except Exception as e:
            return False, None, str(e)

    def remove_customer_door_plan(self, customer_number, plan_id):
        """Remove a door access plan from a customer.

        Returns (success, error_code, message).
        """
        try:
            resp = self._bbts_request(
                "DELETE",
                f"/bbts/api/management/v1"
                f"/customers/{customer_number}/doorAccessPlans/{plan_id}")
            return self._parse_operation_result(resp)
        except Exception as e:
            return False, None, str(e)

    # ── Board / meal plan operations ────────────────────────────────────

    def get_customer_board_plans(self, customer_number):
        """Get board/meal plans currently assigned to a customer."""
        try:
            resp = self._bbts_request(
                "GET",
                f"/bbts/api/management/v1"
                f"/customers/{customer_number}/boardPlans")
            resp.raise_for_status()
            data = resp.json()
            plans = (data.get("customerBoardPlans")
                     or data.get("CustomerBoardPlans") or [])
            return plans, ""
        except Exception as e:
            return [], str(e)

    def add_customer_board_plan(self, customer_number, plan_id, priority=1,
                                active=True, start_date=None, end_date=None):
        """Assign a board/meal plan to a customer.

        Returns (success, error_code, message).
        """
        payload = {
            "customerBoardPlan": {
                "priority": priority,
                "active": active,
                "posOptionShowCounts": True,
                "posOptionPrintCounts": False,
                "boardPlanId": plan_id,
            }
        }
        if start_date:
            payload["customerBoardPlan"]["overridePlanStartDate"] = start_date
        if end_date:
            payload["customerBoardPlan"]["overridePlanStopDate"] = end_date
        try:
            resp = self._bbts_request(
                "POST",
                f"/bbts/api/management/v1"
                f"/customers/{customer_number}/BoardPlans",
                body_dict=payload)
            return self._parse_operation_result(resp)
        except Exception as e:
            return False, None, str(e)

    def remove_customer_board_plan(self, customer_number, plan_id):
        """Remove a board/meal plan from a customer.

        Returns (success, error_code, message).
        """
        try:
            resp = self._bbts_request(
                "DELETE",
                f"/bbts/api/management/v1"
                f"/customers/{customer_number}/boardPlans/{plan_id}")
            return self._parse_operation_result(resp)
        except Exception as e:
            return False, None, str(e)

    # ── Card management ──────────────────────────────────────────────────

    def get_customer_cards(self, customer_number):
        """Get all cards for a customer via BBTS.

        Returns (cards_list, error_message).
        Each card has: cardNumber, cardType, primary, lost, CardStatusType, etc.
        """
        try:
            resp = self._bbts_request(
                "GET",
                f"/bbts/api/management/v1"
                f"/customers/{customer_number}/CardNumbers")
            resp.raise_for_status()
            data = resp.json()
            cards = data.get("cardNumbers") or data.get("CardNumbers") or []
            return cards, ""
        except Exception as e:
            return [], str(e)

    def update_card_status(self, customer_number, card_number,
                           status_type, lost=None, comment=None,
                           primary=None, issue_number=None):
        """Update a card's status and/or other fields.

        status_type: ACTIVE, FROZEN, RETIRED, CUSTOMER_DELETED
        issue_number: e.g. "02", "03"
        Returns (success, error_code, message).
        """
        payload = {"cardNumber": {"CardStatusType": status_type}}
        if lost is not None:
            payload["cardNumber"]["lost"] = lost
        if comment is not None:
            payload["cardNumber"]["CommentText"] = comment
        if primary is not None:
            payload["cardNumber"]["primary"] = primary
        if issue_number is not None:
            payload["cardNumber"]["issueNumber"] = issue_number
        try:
            resp = self._bbts_request(
                "PATCH",
                f"/bbts/api/management/v1"
                f"/customers/{customer_number}/CardNumbers/{card_number}",
                body_dict=payload)
            return self._parse_operation_result(resp)
        except Exception as e:
            return False, None, str(e)

    def update_customer(self, customer_number, active=None,
                        active_start_date=None, active_end_date=None):
        """Update customer fields (activation, dates).

        Returns (success, error_code, message).
        """
        payload = {"Customer": {}}
        if active is not None:
            payload["Customer"]["Active"] = active
        if active_start_date is not None:
            payload["Customer"]["ActiveStartDate"] = active_start_date
        if active_end_date is not None:
            payload["Customer"]["ActiveEndDate"] = active_end_date
        try:
            resp = self._bbts_request(
                "PATCH",
                f"/bbts/api/management/v1/customers/{customer_number}",
                body_dict=payload)
            return self._parse_operation_result(resp)
        except Exception as e:
            return False, None, str(e)

    def get_customer_by_guid(self, customer_guid):
        """Get customer info by GUID (needed for mobile credential lookup)."""
        try:
            resp = self._transact_request(
                "GET", f"/transact/api/v1/customer/{customer_guid}")
            resp.raise_for_status()
            return resp.json(), ""
        except Exception as e:
            return None, str(e)

    def get_mobile_credential(self, customer_guid):
        """Get mobile credential for a customer.

        Returns (credential_dict, error_message).
        """
        try:
            resp = self._transact_request(
                "GET",
                f"/transact/api/v1/card/mobile/credential/{customer_guid}")
            if resp.ok:
                return resp.json(), ""
            return None, f"HTTP {resp.status_code}"
        except Exception as e:
            return None, str(e)

    def update_mobile_credential(self, credential_payload):
        """Update a mobile credential.

        credential_payload should be the full Credential dict.
        Returns (success, error_code, message).
        """
        url = f"{self.base_url}/transact/api/v1/card/mobile/credential"
        body = _json.dumps(credential_payload)
        try:
            uri, headers = self._signed_headers(url, method="PATCH")
            headers["Content-Type"] = "application/json"
            headers["Accept"] = "application/json"
            resp = requests.patch(uri, headers=headers, data=body, timeout=30)
            if resp.ok:
                return True, None, "Success"
            data = resp.json() if resp.text else {}
            msg = data.get("Message") or data.get("message") or f"HTTP {resp.status_code}"
            return False, resp.status_code, msg
        except Exception as e:
            return False, None, str(e)

    # ── Response helpers ────────────────────────────────────────────────

    @staticmethod
    def _parse_operation_result(resp):
        """Parse a BBTS operation response.

        Returns (success, error_code, message).
        """
        data = resp.json()
        result = data.get("operation", {}).get("result", "")
        if resp.ok and result == "OK":
            return True, None, "Success"
        errors = data.get("operation", {}).get("errors", [])
        if errors:
            err = errors[0]
            return False, err.get("code"), err.get("message", "Unknown error")
        return False, resp.status_code, f"HTTP {resp.status_code}: {resp.text[:200]}"
