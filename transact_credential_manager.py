#!/usr/bin/env python3
"""Credential manager for Transact Access Manager.

Uses the `keyring` library to store secrets in the OS-level credential store
(macOS Keychain, Windows Credential Locker, Linux SecretService).
Non-secret settings are persisted to a JSON file on disk.
"""

import json
import os

import keyring

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_DIR = os.path.join(SCRIPT_DIR, ".transact_access_configs")

KEYRING_SERVICE_TRANSACT = "transact-access-manager-transact"
KEYRING_SERVICE_AD = "transact-access-manager-ad"

_TRANSACT_KEYS = ("consumer_key", "consumer_secret", "hostname", "route_scheme", "route_value")
_AD_KEYS = ("server", "username", "password", "use_ssl")

# Default lookup-field configuration. Seeded on first run; editable in Settings.
# Each field maps a user-facing label to an AD/LDAP attribute name.
DEFAULT_LOOKUP_FIELDS = [
    {"label": "CA8 Student ID", "attr": "extensionAttribute8"},
    {"label": "CA2 HR ID",      "attr": "extensionAttribute2"},
    {"label": "UCCS ID",        "attr": "employeeID"},
    {"label": "Username",       "attr": "cn"},
    {"label": "Email",          "attr": "mail"},
]
DEFAULT_CUSTOMER_NUMBER_ATTR = "employeeID"


class TransactCredentialManager:
    """Read / write credentials via keyring and non-secret settings via JSON."""

    def __init__(self):
        self._settings_path = os.path.join(CONFIG_DIR, "settings.json")

    # ── Transact OAuth credentials ──────────────────────────────────────

    def store_transact_creds(self, consumer_key, consumer_secret, hostname,
                             route_scheme, route_value):
        values = (consumer_key, consumer_secret, hostname, route_scheme, route_value)
        for key, value in zip(_TRANSACT_KEYS, values):
            keyring.set_password(KEYRING_SERVICE_TRANSACT, key, value)

    def get_transact_creds(self):
        """Return dict of Transact creds, or None if any key is missing."""
        creds = {}
        for key in _TRANSACT_KEYS:
            val = keyring.get_password(KEYRING_SERVICE_TRANSACT, key)
            if val is None:
                return None
            creds[key] = val
        return creds

    def clear_transact_creds(self):
        for key in _TRANSACT_KEYS:
            try:
                keyring.delete_password(KEYRING_SERVICE_TRANSACT, key)
            except keyring.errors.PasswordDeleteError:
                pass

    # ── AD / LDAP credentials ───────────────────────────────────────────

    def store_ad_creds(self, server, username, password, use_ssl=True):
        values = (server, username, password, str(use_ssl))
        for key, value in zip(_AD_KEYS, values):
            keyring.set_password(KEYRING_SERVICE_AD, key, value)

    def get_ad_creds(self):
        """Return dict of AD creds (use_ssl as bool), or None if any key is missing."""
        creds = {}
        for key in _AD_KEYS:
            val = keyring.get_password(KEYRING_SERVICE_AD, key)
            if val is None:
                return None
            creds[key] = val
        creds["use_ssl"] = creds["use_ssl"] == "True"
        return creds

    def clear_ad_creds(self):
        for key in _AD_KEYS:
            try:
                keyring.delete_password(KEYRING_SERVICE_AD, key)
            except keyring.errors.PasswordDeleteError:
                pass

    # ── Convenience ─────────────────────────────────────────────────────

    def has_transact_creds(self):
        return self.get_transact_creds() is not None

    def has_ad_creds(self):
        return self.get_ad_creds() is not None

    def has_all_credentials(self):
        return self.has_transact_creds() and self.has_ad_creds()

    def clear_all(self):
        self.clear_transact_creds()
        self.clear_ad_creds()

    # ── Non-secret settings (JSON on disk) ──────────────────────────────

    def load_settings(self):
        if not os.path.isfile(self._settings_path):
            return {}
        with open(self._settings_path, "r") as f:
            return json.load(f)

    def save_settings(self, settings):
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(self._settings_path, "w") as f:
            json.dump(settings, f, indent=2)

    # ── Lookup field configuration ──────────────────────────────────────

    def get_lookup_fields(self):
        """Return the configured list of [{"label", "attr"}, ...].

        Falls back to DEFAULT_LOOKUP_FIELDS if not yet configured or invalid.
        """
        settings = self.load_settings()
        raw = settings.get("lookup_fields")
        if not isinstance(raw, list) or not raw:
            return [dict(f) for f in DEFAULT_LOOKUP_FIELDS]
        out = []
        for item in raw:
            if (isinstance(item, dict)
                    and item.get("label") and item.get("attr")):
                out.append({"label": str(item["label"]),
                            "attr": str(item["attr"])})
        return out or [dict(f) for f in DEFAULT_LOOKUP_FIELDS]

    def set_lookup_fields(self, fields):
        """Persist the list of lookup fields.

        ``fields`` is a list of {"label", "attr"} dicts.
        """
        settings = self.load_settings()
        settings["lookup_fields"] = [
            {"label": str(f["label"]), "attr": str(f["attr"])}
            for f in fields
            if f.get("label") and f.get("attr")
        ]
        self.save_settings(settings)

    def get_customer_number_attr(self):
        """Return the AD attribute that maps to Transact CustomerNumber."""
        settings = self.load_settings()
        attr = settings.get("customer_number_attr")
        if attr:
            return str(attr)
        # Fall back: if the default is among configured fields, use it;
        # otherwise use the first configured field's attribute.
        fields = self.get_lookup_fields()
        attrs = [f["attr"] for f in fields]
        if DEFAULT_CUSTOMER_NUMBER_ATTR in attrs:
            return DEFAULT_CUSTOMER_NUMBER_ATTR
        return attrs[0] if attrs else DEFAULT_CUSTOMER_NUMBER_ATTR

    def set_customer_number_attr(self, attr):
        settings = self.load_settings()
        settings["customer_number_attr"] = str(attr)
        self.save_settings(settings)
