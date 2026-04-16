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
