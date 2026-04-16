#!/usr/bin/env python3
"""Cross-platform Active Directory lookup client.

Uses ``ldap3`` on macOS/Linux and ``pyad`` on Windows, following the same
pattern established in sid_access_group_ui.py.
"""

import sys

# Optional imports — availability checked at runtime
try:
    import pyad.adquery
    _pyad_available = True
except ImportError:
    _pyad_available = False

try:
    import ldap3
    _ldap3_available = True
except ImportError:
    _ldap3_available = False

# Maps human-readable field names to AD attribute names.
AD_LOOKUP_FIELDS = {
    "CA8 Student ID": "extensionAttribute8",
    "CA2 HR ID": "extensionAttribute2",
    "UCCS ID": "employeeID",
    "Username": "cn",
    "Email": "mail",
}

# Attributes we always want back from AD for display/mapping purposes.
RETURN_ATTRS = ["employeeID", "displayName", "mail"]


class ADLookupClient:
    """Perform AD queries, returning employeeID + display info."""

    def __init__(self, server, username, password, use_ssl=True,
                 base_dn="dc=uccs,dc=edu"):
        self.server_addr = server
        self.username = username
        self.password = password
        self.use_ssl = use_ssl
        self.base_dn = base_dn
        self._conn = None
        self._use_pyad = False
        self.last_error = ""   # Last error message for diagnostics
        self.last_query = ""   # Last search filter used (for debugging)

    # ── Connection management ───────────────────────────────────────────

    def connect(self):
        """Establish the AD connection.

        Returns (True, "") on success or (False, error_message) on failure.
        """
        # On Windows, try pyad first (uses current domain session)
        if sys.platform == "win32" and _pyad_available:
            try:
                # Quick test query to verify pyad works
                q = pyad.adquery.ADQuery()
                q.execute_query(attributes=["cn"],
                                where_clause="cn='Administrator'")
                list(q.get_results())  # force execution
                self._use_pyad = True
                self.last_error = ""
                return True, ""
            except Exception as e:
                # pyad failed — fall through to ldap3
                pyad_err = str(e)

        # Try ldap3 (cross-platform)
        if _ldap3_available:
            try:
                import ssl
                tls_config = (ldap3.Tls(validate=ssl.CERT_NONE)
                              if self.use_ssl else None)
                server = ldap3.Server(
                    self.server_addr,
                    get_info=ldap3.ALL,
                    use_ssl=self.use_ssl,
                    tls=tls_config,
                )
                self._conn = ldap3.Connection(
                    server,
                    user=self.username,
                    password=self.password,
                    authentication=ldap3.SIMPLE,
                    auto_bind=True,
                )
                self._use_pyad = False
                self.last_error = ""
                return True, ""
            except Exception as e:
                self._conn = None
                self.last_error = str(e)
                return False, str(e)

        # Neither library worked
        if sys.platform == "win32" and _pyad_available:
            self.last_error = f"pyad failed: {pyad_err}"
            return False, self.last_error

        self.last_error = ("No AD library available. "
                           "Install ldap3: pip install ldap3")
        return False, self.last_error

    def disconnect(self):
        if self._conn:
            try:
                self._conn.unbind()
            except Exception:
                pass
            self._conn = None

    # ── Single lookup ───────────────────────────────────────────────────

    def lookup(self, identifier, ad_field, extra_attrs=None):
        """Look up a single identifier against the given AD attribute.

        Returns dict of attribute values if found, else None.
        Sets self.last_error on failure.
        """
        attrs = list(RETURN_ATTRS)
        if extra_attrs:
            attrs = list(dict.fromkeys(attrs + extra_attrs))

        if self._use_pyad:
            results = self._lookup_pyad_multi(identifier, ad_field, attrs, 1)
            return results[0] if results else None
        return self._lookup_ldap3(identifier, ad_field, attrs)

    def lookup_multi(self, identifier, ad_field, extra_attrs=None, limit=50):
        """Look up an identifier that may contain wildcards (e.g. ``jtay*``).

        Returns a list of dicts (up to *limit*) for all matches.
        Sets self.last_error on failure.
        """
        attrs = list(RETURN_ATTRS)
        if extra_attrs:
            attrs = list(dict.fromkeys(attrs + extra_attrs))

        if self._use_pyad:
            return self._lookup_pyad_multi(identifier, ad_field, attrs, limit)
        return self._lookup_ldap3_multi(identifier, ad_field, attrs, limit)

    # ── pyad implementation ─────────────────────────────────────────────

    def _lookup_pyad_multi(self, identifier, ad_field, attrs, limit):
        """Query AD via pyad, returning up to *limit* results."""
        where = f"{ad_field}='{identifier}'"
        self.last_query = f"pyad: {where}"
        try:
            query = pyad.adquery.ADQuery()
            query.execute_query(
                attributes=attrs,
                where_clause=where,
            )
            results = []
            for entry in query.get_results():
                row = {a: entry.get(a, "") for a in attrs}
                results.append(row)
                if len(results) >= limit:
                    break
            self.last_error = ""
            return results
        except Exception as e:
            self.last_error = f"pyad query error: {e}"
            return []

    # ── ldap3 implementation ────────────────────────────────────────────

    def _lookup_ldap3(self, identifier, ad_field, attrs):
        if not self._conn:
            self.last_error = "No LDAP connection"
            return None
        for attempt in range(2):
            try:
                search_filter = f"({ad_field}={identifier})"
                self.last_query = f"ldap3: {search_filter} base={self.base_dn}"
                self._conn.search(self.base_dn, search_filter, attributes=attrs)
                if self._conn.entries:
                    entry = self._conn.entries[0]
                    result = {}
                    for a in attrs:
                        val = entry[a].value if a in entry else ""
                        result[a] = val if val is not None else ""
                    self.last_error = ""
                    return result
                self.last_error = ""
                return None  # Search succeeded but no results
            except Exception as e:
                self.last_error = f"LDAP error: {e}"
                if attempt == 0:
                    self._reconnect()
                else:
                    return None
        return None

    def _lookup_ldap3_multi(self, identifier, ad_field, attrs, limit):
        if not self._conn:
            self.last_error = "No LDAP connection"
            return []
        for attempt in range(2):
            try:
                search_filter = f"({ad_field}={identifier})"
                self.last_query = f"ldap3: {search_filter} base={self.base_dn}"
                self._conn.search(
                    self.base_dn, search_filter, attributes=attrs)
                results = []
                for entry in self._conn.entries:
                    row = {}
                    for a in attrs:
                        val = entry[a].value if a in entry else ""
                        row[a] = val if val is not None else ""
                    results.append(row)
                self.last_error = ""
                return results
            except Exception as e:
                self.last_error = f"LDAP error: {e}"
                if attempt == 0:
                    self._reconnect()
                else:
                    return []
        return []

    def _reconnect(self):
        """Attempt to re-establish the LDAP connection."""
        self.disconnect()
        try:
            self.connect()
        except Exception as e:
            self.last_error = f"Reconnect failed: {e}"

    # ── Batch lookup ────────────────────────────────────────────────────

    def lookup_batch(self, identifiers, ad_field, progress_callback=None):
        """Resolve a list of identifiers against AD.

        Parameters
        ----------
        identifiers : list[str]
            Raw identifiers from the input file.
        ad_field : str
            AD attribute name to query (e.g. ``extensionAttribute8``).
        progress_callback : callable(current, total) | None
            Called after each lookup with (current_index, total_count).

        Returns
        -------
        list[dict | None]
            Parallel list — dict of attributes for matches, None for misses.
        """
        total = len(identifiers)
        results = []
        for i, ident in enumerate(identifiers):
            ident = ident.strip()
            if ident:
                results.append(self.lookup(ident, ad_field))
            else:
                results.append(None)
            if progress_callback:
                progress_callback(i + 1, total)
        return results
