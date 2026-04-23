#!/usr/bin/env python3
"""Transact Access Manager — manage door access and board/meal plans via API.

Loads user identifiers from CSV/text, resolves them through Active Directory,
then stages and commits add/remove operations directly against the Transact
BBTS Management API, eliminating the manual CSV-import step.
"""

import csv
import datetime
import io
import json
import os
import sys
import threading
import tkinter as tk
from dataclasses import dataclass, field
from tkinter import ttk, filedialog, messagebox

from ad_lookup import ADLookupClient, RETURN_ATTRS
from transact_api import TransactOAuthClient
from transact_credential_manager import TransactCredentialManager

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

DELIMITER_MAP = {
    "Auto": None,
    "Comma": ",",
    "Tab": "\t",
    "Pipe": "|",
    "Semicolon": ";",
}


# ── Treeview column sort helper ──────────────────────────────────────────────

def _treeview_sort_setup(tree):
    """Attach click-to-sort on all columns of a Treeview.

    Cycles: ascending → descending → original order.
    Stores the original insertion order so it can be restored.
    """
    tree._sort_state = {}  # col -> "asc" | "desc" | None
    tree._original_order = []  # snapshot taken on first sort

    def _sort_by(col):
        state = tree._sort_state.get(col)

        # Snapshot original order on first ever sort
        if not tree._original_order:
            tree._original_order = list(tree.get_children(""))

        # Clear indicator on all columns
        for c in tree["columns"]:
            tree.heading(c, text=tree.heading(c, "text").rstrip(" ↑↓"))
        tree._sort_state = {c: None for c in tree["columns"]}

        if state is None or state == "desc":
            # Sort ascending
            reverse = False
            new_state = "asc"
            indicator = " ↑"
        elif state == "asc":
            # Sort descending
            reverse = True
            new_state = "desc"
            indicator = " ↓"

        if new_state in ("asc", "desc"):
            col_idx = list(tree["columns"]).index(col)
            items = [(tree.item(iid, "values"), tree.item(iid, "tags"), iid)
                     for iid in tree.get_children("")]

            def sort_key(item):
                val = item[0][col_idx] if col_idx < len(item[0]) else ""
                # Try numeric sort first
                try:
                    return (0, float(val))
                except (ValueError, TypeError):
                    return (1, str(val).lower())

            items.sort(key=sort_key, reverse=reverse)
            for i, (vals, tags, iid) in enumerate(items):
                tree.move(iid, "", i)

            base_text = tree.heading(col, "text").rstrip(" ↑↓")
            tree.heading(col, text=base_text + indicator)
            tree._sort_state[col] = new_state

    for col in tree["columns"]:
        tree.heading(col, command=lambda c=col: _sort_by(c))


# ── Data model ──────────────────────────────────────────────────────────────

@dataclass
class ResolvedUser:
    """A user whose identifier has been resolved via AD."""
    identifier: str
    display_name: str
    customer_number: str    # value of the configured customer-number attribute
    email: str
    row_index: int          # index in input data for reference
    extra: dict = field(default_factory=dict)  # any additional AD attrs fetched


@dataclass
class StagedAction:
    """One planned API operation."""
    user: ResolvedUser
    action: str             # "Add" or "Remove"
    plan_type: str          # "Door Access" or "Board/Meal"
    plan_name: str
    plan_id: int
    status: str = "Pending"
    error_message: str = ""
    current_state: str = "" # "Has plan" / "No plan" / "" (unknown)
    # Board-plan-specific fields
    priority: int = 1
    active: bool = True
    start_date: str = ""
    end_date: str = ""


@dataclass
class StagedCardAction:
    """One planned card or customer operation."""
    customer_number: str
    display_name: str
    action_type: str        # "Card Status", "Card Lost", "Card Issue", "Customer Active"
    card_number: str = ""
    detail: str = ""        # e.g. "ACTIVE -> RETIRED", "True -> False"
    reason: str = ""        # CommentText for retirement
    new_value: str = ""     # The new value to set
    old_value: str = ""     # The original value
    status: str = "Pending"
    error_message: str = ""


# ── Credential dialog ──────────────────────────────────────────────────────

class TransactCredentialDialog(tk.Toplevel):
    """Three-tab modal dialog for Transact OAuth, AD/LDAP, and lookup fields."""

    def __init__(self, parent, transact_creds=None, ad_creds=None,
                 lookup_fields=None, customer_number_attr=None):
        super().__init__(parent)
        self.title("Settings")
        self.geometry("560x500")
        self.resizable(True, True)
        self.minsize(520, 460)
        self.transient(parent)
        self.grab_set()

        self.result = None
        self._lookup_fields = [dict(f) for f in (lookup_fields or [])]
        self._customer_attr = customer_number_attr or ""

        self.update_idletasks()
        x = parent.winfo_x() + (parent.winfo_width() // 2) - 280
        y = parent.winfo_y() + (parent.winfo_height() // 2) - 250
        self.geometry(f"+{x}+{y}")

        notebook = ttk.Notebook(self)
        notebook.pack(fill=tk.BOTH, expand=True, padx=10, pady=(10, 0))

        # ── Transact tab ────────────────────────────────────────────────
        t_frame = ttk.Frame(notebook, padding=15)
        notebook.add(t_frame, text="Transact API")

        labels_t = ["Hostname:", "Consumer Key:", "Consumer Secret:",
                     "Route Scheme:", "Route ID:"]
        self._t_entries = {}
        defaults = {
            "hostname": (transact_creds or {}).get("hostname", "uccs.tsecloud.net"),
            "consumer_key": (transact_creds or {}).get("consumer_key", ""),
            "consumer_secret": (transact_creds or {}).get("consumer_secret", ""),
            "route_scheme": (transact_creds or {}).get("route_scheme", "InstitutionRouteID"),
            "route_value": (transact_creds or {}).get("route_value", ""),
        }
        keys_t = ["hostname", "consumer_key", "consumer_secret",
                   "route_scheme", "route_value"]
        for i, (label, key) in enumerate(zip(labels_t, keys_t)):
            ttk.Label(t_frame, text=label).grid(row=i, column=0, sticky=tk.W,
                                                 pady=6, padx=(0, 8))
            show = "*" if "secret" in key.lower() else ""
            entry = ttk.Entry(t_frame, width=35, show=show)
            entry.insert(0, defaults.get(key, ""))
            entry.grid(row=i, column=1, sticky=tk.EW, pady=6)
            self._t_entries[key] = entry
        t_frame.columnconfigure(1, weight=1)

        # ── AD tab ──────────────────────────────────────────────────────
        a_frame = ttk.Frame(notebook, padding=15)
        notebook.add(a_frame, text="Active Directory")

        ad_defaults = {
            "server": (ad_creds or {}).get("server", "ldap.uccs.edu"),
            "username": (ad_creds or {}).get("username", ""),
            "password": (ad_creds or {}).get("password", ""),
        }
        labels_a = ["Server:", "Username (DOMAIN\\user):", "Password:"]
        keys_a = ["server", "username", "password"]
        self._a_entries = {}
        for i, (label, key) in enumerate(zip(labels_a, keys_a)):
            ttk.Label(a_frame, text=label).grid(row=i, column=0, sticky=tk.W,
                                                 pady=8, padx=(0, 8))
            show = "*" if key == "password" else ""
            entry = ttk.Entry(a_frame, width=30, show=show)
            entry.insert(0, ad_defaults.get(key, ""))
            entry.grid(row=i, column=1, sticky=tk.EW, pady=8)
            self._a_entries[key] = entry

        self._ssl_var = tk.BooleanVar(
            value=(ad_creds or {}).get("use_ssl", True))
        ttk.Checkbutton(a_frame, text="Use Secure Connection (SSL)",
                        variable=self._ssl_var).grid(
            row=3, column=1, sticky=tk.W, pady=8)
        a_frame.columnconfigure(1, weight=1)

        # ── Lookup Fields tab ───────────────────────────────────────────
        lf_frame = ttk.Frame(notebook, padding=10)
        notebook.add(lf_frame, text="Lookup Fields")

        ttk.Label(
            lf_frame,
            text=("Configure which AD attributes appear as search fields. "
                  "One field — marked ★ — is used as the Transact "
                  "CustomerNumber."),
            wraplength=500, justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(0, 8))

        tree_holder = ttk.Frame(lf_frame)
        tree_holder.pack(fill=tk.BOTH, expand=True)

        self._lf_tree = ttk.Treeview(
            tree_holder, columns=("cust", "label", "attr"),
            show="headings", height=7, selectmode="browse")
        self._lf_tree.heading("cust", text="Customer#")
        self._lf_tree.heading("label", text="Label")
        self._lf_tree.heading("attr", text="AD Attribute")
        self._lf_tree.column("cust", width=80, minwidth=70, anchor=tk.CENTER,
                             stretch=False)
        self._lf_tree.column("label", width=180, minwidth=120)
        self._lf_tree.column("attr", width=200, minwidth=140)

        lf_vsb = ttk.Scrollbar(tree_holder, orient=tk.VERTICAL,
                               command=self._lf_tree.yview)
        self._lf_tree.configure(yscrollcommand=lf_vsb.set)
        self._lf_tree.grid(row=0, column=0, sticky="nsew")
        lf_vsb.grid(row=0, column=1, sticky="ns")
        tree_holder.rowconfigure(0, weight=1)
        tree_holder.columnconfigure(0, weight=1)

        self._lf_tree.bind("<Double-1>", lambda e: self._lf_edit())

        btn_row = ttk.Frame(lf_frame)
        btn_row.pack(fill=tk.X, pady=(6, 0))
        ttk.Button(btn_row, text="Add...",
                   command=self._lf_add).pack(side=tk.LEFT)
        ttk.Button(btn_row, text="Edit...",
                   command=self._lf_edit).pack(side=tk.LEFT, padx=(4, 0))
        ttk.Button(btn_row, text="Remove",
                   command=self._lf_remove).pack(side=tk.LEFT, padx=(4, 0))
        ttk.Button(btn_row, text="Move Up",
                   command=lambda: self._lf_move(-1)).pack(
            side=tk.LEFT, padx=(12, 0))
        ttk.Button(btn_row, text="Move Down",
                   command=lambda: self._lf_move(1)).pack(
            side=tk.LEFT, padx=(4, 0))
        ttk.Button(btn_row, text="Set as Customer #",
                   command=self._lf_set_customer).pack(
            side=tk.RIGHT)

        self._lf_refresh()

        # ── Buttons ─────────────────────────────────────────────────────
        btn_frame = ttk.Frame(self)
        btn_frame.pack(fill=tk.X, padx=10, pady=10)
        ttk.Button(btn_frame, text="Cancel", command=self._cancel).pack(
            side=tk.RIGHT, padx=(5, 0))
        ttk.Button(btn_frame, text="Save", command=self._save,
                   default="active").pack(side=tk.RIGHT)

        self.bind("<Return>", self._save)
        self.bind("<Escape>", self._cancel)

        self._t_entries["hostname"].focus()

    # ── Lookup-field tab helpers ────────────────────────────────────────

    def _lf_refresh(self):
        self._lf_tree.delete(*self._lf_tree.get_children())
        for i, f in enumerate(self._lookup_fields):
            star = "★" if f["attr"] == self._customer_attr else ""
            self._lf_tree.insert("", tk.END, iid=str(i),
                                 values=(star, f["label"], f["attr"]))

    def _lf_selected_index(self):
        sel = self._lf_tree.selection()
        if not sel:
            return None
        try:
            return int(sel[0])
        except ValueError:
            return None

    def _lf_prompt(self, initial_label="", initial_attr=""):
        """Prompt for label + AD attribute. Returns (label, attr) or None."""
        dlg = tk.Toplevel(self)
        dlg.title("Lookup Field")
        dlg.resizable(False, False)
        dlg.transient(self)
        dlg.grab_set()

        dlg.update_idletasks()
        x = self.winfo_x() + (self.winfo_width() // 2) - 170
        y = self.winfo_y() + (self.winfo_height() // 2) - 80
        dlg.geometry(f"340x150+{x}+{y}")

        body = ttk.Frame(dlg, padding=12)
        body.pack(fill=tk.BOTH, expand=True)

        ttk.Label(body, text="Label:").grid(row=0, column=0, sticky=tk.W,
                                            pady=6, padx=(0, 8))
        label_entry = ttk.Entry(body, width=30)
        label_entry.insert(0, initial_label)
        label_entry.grid(row=0, column=1, sticky=tk.EW, pady=6)

        ttk.Label(body, text="AD Attribute:").grid(row=1, column=0, sticky=tk.W,
                                                   pady=6, padx=(0, 8))
        attr_entry = ttk.Entry(body, width=30)
        attr_entry.insert(0, initial_attr)
        attr_entry.grid(row=1, column=1, sticky=tk.EW, pady=6)

        body.columnconfigure(1, weight=1)

        result = [None]

        def _ok(event=None):
            lbl = label_entry.get().strip()
            attr = attr_entry.get().strip()
            if not lbl or not attr:
                messagebox.showwarning("Required",
                                       "Both fields are required.",
                                       parent=dlg)
                return
            result[0] = (lbl, attr)
            dlg.destroy()

        def _cancel(event=None):
            dlg.destroy()

        btns = ttk.Frame(dlg, padding=(12, 0, 12, 12))
        btns.pack(fill=tk.X)
        ttk.Button(btns, text="Cancel", command=_cancel).pack(
            side=tk.RIGHT, padx=(5, 0))
        ttk.Button(btns, text="OK", command=_ok,
                   default="active").pack(side=tk.RIGHT)
        dlg.bind("<Return>", _ok)
        dlg.bind("<Escape>", _cancel)
        label_entry.focus()
        dlg.wait_window()
        return result[0]

    def _lf_add(self):
        picked = self._lf_prompt()
        if not picked:
            return
        label, attr = picked
        if any(f["attr"] == attr for f in self._lookup_fields):
            messagebox.showwarning(
                "Duplicate",
                f"An entry for '{attr}' already exists.", parent=self)
            return
        self._lookup_fields.append({"label": label, "attr": attr})
        if not self._customer_attr:
            self._customer_attr = attr
        self._lf_refresh()

    def _lf_edit(self):
        idx = self._lf_selected_index()
        if idx is None:
            return
        f = self._lookup_fields[idx]
        picked = self._lf_prompt(f["label"], f["attr"])
        if not picked:
            return
        new_label, new_attr = picked
        # Check for attr collision with a different row
        for j, other in enumerate(self._lookup_fields):
            if j != idx and other["attr"] == new_attr:
                messagebox.showwarning(
                    "Duplicate",
                    f"An entry for '{new_attr}' already exists.", parent=self)
                return
        # If we're changing the attr and this row is the customer source,
        # update the customer attr too
        if self._customer_attr == f["attr"]:
            self._customer_attr = new_attr
        self._lookup_fields[idx] = {"label": new_label, "attr": new_attr}
        self._lf_refresh()

    def _lf_remove(self):
        idx = self._lf_selected_index()
        if idx is None:
            return
        f = self._lookup_fields[idx]
        if not messagebox.askyesno(
                "Remove", f"Remove '{f['label']}'?", parent=self):
            return
        removed = self._lookup_fields.pop(idx)
        if removed["attr"] == self._customer_attr:
            self._customer_attr = (self._lookup_fields[0]["attr"]
                                   if self._lookup_fields else "")
        self._lf_refresh()

    def _lf_move(self, delta):
        idx = self._lf_selected_index()
        if idx is None:
            return
        new_idx = idx + delta
        if not (0 <= new_idx < len(self._lookup_fields)):
            return
        self._lookup_fields[idx], self._lookup_fields[new_idx] = (
            self._lookup_fields[new_idx], self._lookup_fields[idx])
        self._lf_refresh()
        self._lf_tree.selection_set(str(new_idx))

    def _lf_set_customer(self):
        idx = self._lf_selected_index()
        if idx is None:
            return
        self._customer_attr = self._lookup_fields[idx]["attr"]
        self._lf_refresh()
        self._lf_tree.selection_set(str(idx))

    def _save(self, event=None):
        if not self._lookup_fields:
            messagebox.showwarning(
                "Lookup Fields",
                "At least one lookup field is required.", parent=self)
            return
        if not self._customer_attr or not any(
                f["attr"] == self._customer_attr for f in self._lookup_fields):
            messagebox.showwarning(
                "Customer #",
                "Mark one field as the Transact CustomerNumber "
                "(use 'Set as Customer #').", parent=self)
            return
        self.result = {
            "transact": {k: e.get().strip() for k, e in self._t_entries.items()},
            "ad": {
                **{k: e.get().strip() for k, e in self._a_entries.items()},
                "use_ssl": self._ssl_var.get(),
            },
            "lookup_fields": [dict(f) for f in self._lookup_fields],
            "customer_number_attr": self._customer_attr,
        }
        # Keep password unstripped
        self.result["ad"]["password"] = self._a_entries["password"].get()
        self.destroy()

    def _cancel(self, event=None):
        self.result = None
        self.destroy()

    def show(self):
        self.wait_window(self)
        return self.result


# ── Main application ────────────────────────────────────────────────────────

class TransactAccessManagerApp(tk.Tk):

    def __init__(self):
        super().__init__()

        self.title("Transact Access Manager")
        self.geometry("1400x900")
        self.minsize(1200, 750)

        style = ttk.Style(self)

        # Bold style for the three key action buttons
        style.configure("Bold.TButton", font=("Helvetica", 10, "bold"))

        # State
        self.cred_manager = TransactCredentialManager()
        self.api_client = None      # TransactOAuthClient
        self.ad_client = None       # ADLookupClient

        # Lookup-field configuration (populated from settings)
        self._lookup_fields = self.cred_manager.get_lookup_fields()
        self._customer_attr = self.cred_manager.get_customer_number_attr()

        self.csv_path = None
        self.csv_headers = []
        self.csv_data = []
        self.data_start_index = 1
        self.detected_delimiter = ","

        self.resolved_users = []    # list[ResolvedUser | None], parallel to csv_data rows
        self.staged_actions = []    # list[StagedAction]

        self.door_plans = []        # raw dicts from API
        self.board_plans = []

        # Audit log path
        self._audit_log_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            ".transact_access_configs", "audit.log")

        # Session log lines (for the history panel)
        self._log_lines = []

        self._build_ui()
        self._load_saved_settings()
        self.after(200, self._startup_sequence)
        self._start_keepalive()

    # ════════════════════════════════════════════════════════════════════
    #  Lookup-field helpers
    # ════════════════════════════════════════════════════════════════════

    def _lookup_labels(self):
        """Ordered list of labels for use in comboboxes."""
        return [f["label"] for f in self._lookup_fields]

    def _attr_for_label(self, label):
        """Return the AD attribute for a given label, or None."""
        for f in self._lookup_fields:
            if f["label"] == label:
                return f["attr"]
        return None

    def _label_for_attr(self, attr):
        """Return the label for a given AD attribute, or None."""
        for f in self._lookup_fields:
            if f["attr"] == attr:
                return f["label"]
        return None

    def _customer_label(self):
        """Label of the configured customer-number field (or the attr name)."""
        return self._label_for_attr(self._customer_attr) or self._customer_attr

    def _all_lookup_attrs(self):
        """All AD attributes referenced by lookup fields + customer-number."""
        attrs = [f["attr"] for f in self._lookup_fields]
        if self._customer_attr not in attrs:
            attrs.append(self._customer_attr)
        return attrs

    def _rebuild_single_info_panel(self):
        """Populate the AD Info panel using configured lookup fields.

        Rows: Name, Email, plus each configured lookup field. The configured
        customer-number field is marked with " ★".
        """
        if not hasattr(self, "_single_info_frame"):
            return
        for child in self._single_info_frame.winfo_children():
            child.destroy()
        self._single_result_vars = {}

        # Always-present fields
        always = [("Name", "displayName"), ("Email", "mail")]
        rows = list(always)
        seen_attrs = {a for _, a in always}
        for f in self._lookup_fields:
            if f["attr"] in seen_attrs:
                continue
            label = f["label"]
            if f["attr"] == self._customer_attr:
                label += "  ★"
            rows.append((label, f["attr"]))
            seen_attrs.add(f["attr"])
        # If customer-number attr isn't in lookup fields at all, still show it
        if self._customer_attr not in seen_attrs:
            rows.append((f"{self._customer_attr}  ★", self._customer_attr))

        for i, (label, attr) in enumerate(rows):
            ttk.Label(self._single_info_frame, text=f"{label}:").grid(
                row=i, column=0, sticky=tk.W, padx=(0, 6), pady=1)
            var = tk.StringVar()
            entry = ttk.Entry(self._single_info_frame, textvariable=var,
                              state="readonly", width=30)
            entry.grid(row=i, column=1, sticky=tk.EW, pady=1)
            self._single_result_vars[attr] = var
        self._single_info_frame.columnconfigure(1, weight=1)

    def _apply_lookup_field_changes(self):
        """Refresh UI widgets that depend on the lookup-field config."""
        labels = self._lookup_labels()

        # Preserve current selections where possible
        prev_bulk = self.ad_field_combo.get() if hasattr(self, "ad_field_combo") else ""
        prev_single = (self.single_ad_field_combo.get()
                       if hasattr(self, "single_ad_field_combo") else "")

        if hasattr(self, "ad_field_combo"):
            self.ad_field_combo["values"] = labels
            if prev_bulk in labels:
                self.ad_field_combo.set(prev_bulk)
            elif labels:
                self.ad_field_combo.current(0)
            else:
                self.ad_field_combo.set("")

        if hasattr(self, "single_ad_field_combo"):
            self.single_ad_field_combo["values"] = labels
            if prev_single in labels:
                self.single_ad_field_combo.set(prev_single)
            elif labels:
                # Prefer Username default
                idx = next((i for i, l in enumerate(labels)
                            if l.lower() == "username"), 0)
                self.single_ad_field_combo.current(idx)
            else:
                self.single_ad_field_combo.set("")

        self._rebuild_single_info_panel()

    # ════════════════════════════════════════════════════════════════════
    #  UI construction
    # ════════════════════════════════════════════════════════════════════

    def _build_ui(self):
        # ── Top bar: connection status panels + settings ────────────────
        top_bar = ttk.Frame(self)
        top_bar.pack(side=tk.TOP, fill=tk.X, padx=10, pady=(8, 0))

        # Transact status panel
        self.transact_status_var = tk.StringVar(value="Transact: Not connected")
        self.transact_status_lbl = tk.Label(
            top_bar, textvariable=self.transact_status_var,
            fg="white", bg="#cc3333", font=("Helvetica", 10, "bold"),
            padx=10, pady=4, anchor="w")
        self.transact_status_lbl.pack(side=tk.LEFT, fill=tk.X, expand=True)

        # AD status panel
        self.ad_status_var = tk.StringVar(value="AD: Not connected")
        self.ad_status_lbl = tk.Label(
            top_bar, textvariable=self.ad_status_var,
            fg="white", bg="#cc3333", font=("Helvetica", 10, "bold"),
            padx=10, pady=4, anchor="w")
        self.ad_status_lbl.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(2, 0))

        ttk.Button(top_bar, text="About",
                   command=self._show_about).pack(side=tk.RIGHT, padx=(4, 0))
        ttk.Button(top_bar, text="Settings...",
                   command=self._open_settings).pack(side=tk.RIGHT, padx=(4, 0))

        # ── Main PanedWindow (top resizable | middle fixed | bottom resizable)
        main_pane = tk.PanedWindow(self, orient=tk.VERTICAL, sashrelief=tk.RAISED,
                                   sashwidth=5, bg="#cccccc")
        main_pane.pack(fill=tk.BOTH, expand=True, padx=10, pady=(4, 0))

        # ════════════════════════════════════════════════════════════════
        #  TOP PANE — User input notebook (Bulk CSV / Single User)
        # ════════════════════════════════════════════════════════════════
        top_pane_frame = ttk.Frame(main_pane)
        main_pane.add(top_pane_frame, minsize=200, height=520, stretch="always")

        self.input_notebook = ttk.Notebook(top_pane_frame)
        self.input_notebook.pack(fill=tk.BOTH, expand=True)

        # ── Tab 1: Bulk CSV ─────────────────────────────────────────────
        bulk_tab = ttk.Frame(self.input_notebook, padding=5)
        self.input_notebook.add(bulk_tab, text="Bulk (CSV)")

        # Row 1: file selection
        row1 = ttk.Frame(bulk_tab)
        row1.pack(fill=tk.X, pady=(0, 4))

        ttk.Button(row1, text="Browse File...",
                   command=self._browse_file).pack(side=tk.LEFT, padx=(0, 8))
        self.file_lbl_var = tk.StringVar(value="No file selected.")
        ttk.Label(row1, textvariable=self.file_lbl_var,
                  foreground="gray").pack(side=tk.LEFT, fill=tk.X, expand=True)

        self.has_header_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(row1, text="First row is header",
                        variable=self.has_header_var,
                        command=self._on_header_toggle).pack(side=tk.RIGHT, padx=(8, 0))

        ttk.Label(row1, text="Delimiter:").pack(side=tk.RIGHT, padx=(8, 2))
        self.delim_var = tk.StringVar(value="Auto")
        ttk.Combobox(row1, textvariable=self.delim_var,
                     values=list(DELIMITER_MAP.keys()),
                     state="readonly", width=8).pack(side=tk.RIGHT)

        # Row 2: mapping + resolve
        row2 = ttk.Frame(bulk_tab)
        row2.pack(fill=tk.X, pady=(0, 4))

        ttk.Label(row2, text="Identifier Column:").pack(side=tk.LEFT, padx=(0, 4))
        self.col_combo = ttk.Combobox(row2, state="readonly", width=20)
        self.col_combo.pack(side=tk.LEFT, padx=(0, 12))

        ttk.Label(row2, text="AD Field:").pack(side=tk.LEFT, padx=(0, 4))
        self.ad_field_combo = ttk.Combobox(
            row2, values=self._lookup_labels(),
            state="readonly", width=18)
        self.ad_field_combo.pack(side=tk.LEFT, padx=(0, 12))
        if self._lookup_fields:
            self.ad_field_combo.current(0)

        self.resolve_btn = ttk.Button(row2, text="Resolve Users",
                                      command=self._resolve_users,
                                      style="Bold.TButton")
        self.resolve_btn.pack(side=tk.LEFT, padx=(0, 12))

        self.resolve_status_var = tk.StringVar(value="")
        ttk.Label(row2, textvariable=self.resolve_status_var,
                  foreground="gray").pack(side=tk.LEFT)

        # Input preview tree
        tree_frame = ttk.Frame(bulk_tab)
        tree_frame.pack(fill=tk.BOTH, expand=True, pady=(4, 0))

        self.input_tree = ttk.Treeview(tree_frame, show="headings", height=5)
        input_vsb = ttk.Scrollbar(tree_frame, orient=tk.VERTICAL,
                                  command=self.input_tree.yview)
        input_hsb = ttk.Scrollbar(tree_frame, orient=tk.HORIZONTAL,
                                  command=self.input_tree.xview)
        self.input_tree.configure(yscrollcommand=input_vsb.set,
                                  xscrollcommand=input_hsb.set)
        self.input_tree.grid(row=0, column=0, sticky="nsew")
        input_vsb.grid(row=0, column=1, sticky="ns")
        input_hsb.grid(row=1, column=0, sticky="ew")
        tree_frame.rowconfigure(0, weight=1)
        tree_frame.columnconfigure(0, weight=1)
        _treeview_sort_setup(self.input_tree)

        # ── Tab 2: Single User Lookup ───────────────────────────────────
        single_tab = ttk.Frame(self.input_notebook, padding=5)
        self.input_notebook.add(single_tab, text="Single User Lookup")

        # Search row
        search_row = ttk.Frame(single_tab)
        search_row.pack(fill=tk.X, pady=(0, 6))

        ttk.Label(search_row, text="Search By:").pack(side=tk.LEFT, padx=(0, 4))
        self.single_ad_field_combo = ttk.Combobox(
            search_row, values=self._lookup_labels(),
            state="readonly", width=18)
        self.single_ad_field_combo.pack(side=tk.LEFT, padx=(0, 12))
        # Default to "Username" if present, else the first field
        default_idx = 0
        for i, lbl in enumerate(self._lookup_labels()):
            if lbl.lower() == "username":
                default_idx = i
                break
        if self._lookup_fields:
            self.single_ad_field_combo.current(default_idx)

        ttk.Label(search_row, text="Value:").pack(side=tk.LEFT, padx=(0, 4))
        self.single_search_var = tk.StringVar()
        self.single_search_entry = ttk.Entry(search_row,
                                             textvariable=self.single_search_var,
                                             width=25)
        self.single_search_entry.pack(side=tk.LEFT, padx=(0, 8))
        self.single_search_entry.bind("<Return>", lambda e: self._single_lookup())

        self.single_lookup_btn = ttk.Button(
            search_row, text="Lookup", command=self._single_lookup)
        self.single_lookup_btn.pack(side=tk.LEFT, padx=(0, 12))

        self.single_status_var = tk.StringVar(value="")
        ttk.Label(search_row, textvariable=self.single_status_var,
                  foreground="gray").pack(side=tk.LEFT)

        # ── Two-pane result area: Left = AD + Plans, Right = Cards ──────
        result_pane = tk.PanedWindow(single_tab, orient=tk.HORIZONTAL,
                                     sashrelief=tk.RAISED, sashwidth=4)
        result_pane.pack(fill=tk.BOTH, expand=True, pady=(4, 0))

        # ── LEFT HALF: AD info + current plans ──────────────────────────
        left_frame = ttk.Frame(result_pane)
        result_pane.add(left_frame, minsize=250, stretch="always")

        # AD result fields — built dynamically from configured lookup fields
        self._single_info_frame = ttk.Labelframe(left_frame, text="AD Info",
                                                 padding=5)
        self._single_info_frame.pack(fill=tk.X)
        self._single_result_vars = {}
        self._single_info_left_frame = left_frame  # for future rebuilds
        self._rebuild_single_info_panel()

        # Current plans
        # Labelwidget: title + remove button on the same row as the frame border
        plans_label_frame = ttk.Frame(left_frame)
        ttk.Label(plans_label_frame, text=" Current Plans ",
                  font=("Helvetica", 9, "bold")).pack(side=tk.LEFT)
        self.single_remove_btn = ttk.Button(
            plans_label_frame, text="Remove Selected",
            command=self._single_remove_plan)
        self.single_remove_btn.pack(side=tk.LEFT, padx=(4, 0))

        plans_frame = ttk.Labelframe(left_frame, labelwidget=plans_label_frame,
                                     padding=5)
        plans_frame.pack(fill=tk.BOTH, expand=True, pady=(4, 0))

        plans_tree_frame = ttk.Frame(plans_frame)
        plans_tree_frame.pack(fill=tk.BOTH, expand=True, pady=(2, 0))

        self.single_plans_tree = ttk.Treeview(
            plans_tree_frame, columns=("type", "plan"),
            show="headings", height=5)
        self.single_plans_tree.heading("type", text="T")
        self.single_plans_tree.heading("plan", text="Plan Name")
        self.single_plans_tree.column("type", width=25, minwidth=25, stretch=False)
        self.single_plans_tree.column("plan", width=250, minwidth=100)

        plans_vsb = ttk.Scrollbar(plans_tree_frame, orient=tk.VERTICAL,
                                  command=self.single_plans_tree.yview)
        self.single_plans_tree.configure(yscrollcommand=plans_vsb.set)
        self.single_plans_tree.grid(row=0, column=0, sticky="nsew")
        plans_vsb.grid(row=0, column=1, sticky="ns")
        plans_tree_frame.rowconfigure(0, weight=1)
        plans_tree_frame.columnconfigure(0, weight=1)
        _treeview_sort_setup(self.single_plans_tree)

        # ── RIGHT HALF: Cards ───────────────────────────────────────────
        right_frame = ttk.Frame(result_pane)
        result_pane.add(right_frame, minsize=250, stretch="always")

        cards_frame = ttk.Labelframe(right_frame, text="Cards", padding=5)
        cards_frame.pack(fill=tk.BOTH, expand=True)

        # Toggle bar: view mode + hide retired
        cards_toggle_row = ttk.Frame(cards_frame)
        cards_toggle_row.pack(fill=tk.X, pady=(0, 4))

        self.card_view_var = tk.StringVar(value="table")
        ttk.Radiobutton(cards_toggle_row, text="Table",
                        variable=self.card_view_var, value="table",
                        command=self._toggle_card_view).pack(
            side=tk.LEFT, padx=(0, 4))
        ttk.Radiobutton(cards_toggle_row, text="Raw JSON",
                        variable=self.card_view_var, value="raw",
                        command=self._toggle_card_view).pack(
            side=tk.LEFT, padx=(0, 8))

        self.hide_retired_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(cards_toggle_row, text="Hide Retired",
                        variable=self.hide_retired_var,
                        command=self._apply_card_filter).pack(side=tk.RIGHT)

        # Container that swaps between table and raw views
        self._cards_view_container = ttk.Frame(cards_frame)
        self._cards_view_container.pack(fill=tk.BOTH, expand=True)

        # -- Table view --
        self._cards_table_frame = ttk.Frame(self._cards_view_container)
        self._cards_table_frame.pack(fill=tk.BOTH, expand=True)

        cards_tree_frame = ttk.Frame(self._cards_table_frame)
        cards_tree_frame.pack(fill=tk.BOTH, expand=True)

        card_cols = ("card_num", "issue", "type", "status", "primary", "lost")
        self.cards_tree = ttk.Treeview(
            cards_tree_frame, columns=card_cols, show="headings", height=5)
        self.cards_tree.heading("card_num", text="Card Number")
        self.cards_tree.heading("issue", text="Iss#")
        self.cards_tree.heading("type", text="Type")
        self.cards_tree.heading("status", text="Status")
        self.cards_tree.heading("primary", text="Pri")
        self.cards_tree.heading("lost", text="Lost")
        self.cards_tree.column("card_num", width=120, minwidth=80)
        self.cards_tree.column("issue", width=35, minwidth=30)
        self.cards_tree.column("type", width=70, minwidth=50)
        self.cards_tree.column("status", width=70, minwidth=50)
        self.cards_tree.column("primary", width=35, minwidth=30)
        self.cards_tree.column("lost", width=35, minwidth=30)

        cards_vsb = ttk.Scrollbar(cards_tree_frame, orient=tk.VERTICAL,
                                  command=self.cards_tree.yview)
        self.cards_tree.configure(yscrollcommand=cards_vsb.set)
        self.cards_tree.grid(row=0, column=0, sticky="nsew")
        cards_vsb.grid(row=0, column=1, sticky="ns")
        cards_tree_frame.rowconfigure(0, weight=1)
        cards_tree_frame.columnconfigure(0, weight=1)
        _treeview_sort_setup(self.cards_tree)

        # Tag for modified rows
        self.cards_tree.tag_configure("modified", background="#fff3cd")

        # Editable column definitions
        # "combo" cols get a dropdown, "spin" cols get a spinbox
        self._card_combo_cols = {
            "status": ["ACTIVE", "FROZEN", "RETIRED"],
            "lost": ["True", "False"],
        }
        self._card_spin_cols = {"issue"}  # edited via spinbox
        # Track original values per iid for change detection
        self._card_original_values = {}  # iid -> tuple of original values

        # Double-click to edit
        self.cards_tree.bind("<Double-1>", self._card_on_double_click)

        # -- Raw JSON view --
        self._cards_raw_frame = ttk.Frame(self._cards_view_container)
        # (not packed — hidden by default)

        self.cards_raw_text = tk.Text(self._cards_raw_frame, wrap=tk.WORD,
                                      font=("Courier", 10), state=tk.DISABLED)
        raw_vsb = ttk.Scrollbar(self._cards_raw_frame, orient=tk.VERTICAL,
                                command=self.cards_raw_text.yview)
        self.cards_raw_text.configure(yscrollcommand=raw_vsb.set)
        self.cards_raw_text.grid(row=0, column=0, sticky="nsew")
        raw_vsb.grid(row=0, column=1, sticky="ns")
        self._cards_raw_frame.rowconfigure(0, weight=1)
        self._cards_raw_frame.columnconfigure(0, weight=1)

        # Store all cards for filtering and raw display
        self._all_cards_data = []
        self._all_cards_raw = []     # raw dicts from API
        self._mobile_cred_raw = None # raw mobile credential response

        # Card action row: stage + customer toggle + reason
        card_btn_row = ttk.Frame(cards_frame)
        card_btn_row.pack(fill=tk.X, pady=(4, 0))

        ttk.Button(card_btn_row, text="Stage Card Changes",
                   command=self._stage_card_changes).pack(
            side=tk.LEFT, padx=(0, 4))

        ttk.Separator(card_btn_row, orient=tk.VERTICAL).pack(
            side=tk.LEFT, fill=tk.Y, padx=4)

        ttk.Button(card_btn_row, text="Retire Mobile ID",
                   command=self._stage_retire_mobile).pack(
            side=tk.LEFT, padx=(0, 4))

        ttk.Separator(card_btn_row, orient=tk.VERTICAL).pack(
            side=tk.LEFT, fill=tk.Y, padx=4)

        ttk.Button(card_btn_row, text="Deactivate Customer",
                   command=lambda: self._stage_customer_active(False)).pack(
            side=tk.LEFT, padx=(0, 4))
        ttk.Button(card_btn_row, text="Activate Customer",
                   command=lambda: self._stage_customer_active(True)).pack(
            side=tk.LEFT, padx=(0, 4))

        # Retirement reason entry
        card_btn_row2 = ttk.Frame(cards_frame)
        card_btn_row2.pack(fill=tk.X, pady=(2, 0))

        ttk.Label(card_btn_row2, text="Retire Reason:").pack(
            side=tk.LEFT, padx=(0, 4))
        self.retire_reason_var = tk.StringVar(
            value="Retired via Transact Access Manager")
        ttk.Entry(card_btn_row2, textvariable=self.retire_reason_var,
                  width=40).pack(side=tk.LEFT, padx=(0, 8))

        self.card_status_var = tk.StringVar(
            value="Double-click Status, Lost, or Iss# to edit")
        ttk.Label(card_btn_row2, textvariable=self.card_status_var,
                  foreground="gray").pack(side=tk.LEFT)

        # ── Staged card/customer actions queue ──────────────────────────
        card_stage_label = ttk.Frame(cards_frame)
        ttk.Label(card_stage_label, text=" Queued Actions ",
                  font=("Helvetica", 9, "bold")).pack(side=tk.LEFT)
        self.commit_card_queue_btn = ttk.Button(
            card_stage_label, text="Commit Queue",
            command=self._commit_card_queue, style="Bold.TButton",
            state=tk.DISABLED)
        self.commit_card_queue_btn.pack(side=tk.LEFT, padx=(4, 0))
        ttk.Button(card_stage_label, text="Clear",
                   command=self._clear_card_queue).pack(side=tk.LEFT, padx=(4, 0))

        card_queue_frame = ttk.Labelframe(cards_frame,
                                          labelwidget=card_stage_label,
                                          padding=3)
        card_queue_frame.pack(fill=tk.BOTH, expand=True, pady=(4, 0))

        cq_cols = ("emp_id", "type", "card", "detail", "status")
        self.card_queue_tree = ttk.Treeview(
            card_queue_frame, columns=cq_cols, show="headings", height=3)
        self.card_queue_tree.heading("emp_id", text="EmpID")
        self.card_queue_tree.heading("type", text="Action")
        self.card_queue_tree.heading("card", text="Card")
        self.card_queue_tree.heading("detail", text="Detail")
        self.card_queue_tree.heading("status", text="Status")
        self.card_queue_tree.column("emp_id", width=80, minwidth=50)
        self.card_queue_tree.column("type", width=90, minwidth=60)
        self.card_queue_tree.column("card", width=90, minwidth=50)
        self.card_queue_tree.column("detail", width=150, minwidth=80)
        self.card_queue_tree.column("status", width=80, minwidth=50)

        cq_vsb = ttk.Scrollbar(card_queue_frame, orient=tk.VERTICAL,
                                command=self.card_queue_tree.yview)
        self.card_queue_tree.configure(yscrollcommand=cq_vsb.set)
        self.card_queue_tree.grid(row=0, column=0, sticky="nsew")
        cq_vsb.grid(row=0, column=1, sticky="ns")
        card_queue_frame.rowconfigure(0, weight=1)
        card_queue_frame.columnconfigure(0, weight=1)
        _treeview_sort_setup(self.card_queue_tree)

        # "pending" uses the default tree foreground so it adapts to
        # light/dark mode. Other states use distinct colors that stay
        # readable on both backgrounds.
        self.card_queue_tree.tag_configure("success", foreground="#228B22")
        self.card_queue_tree.tag_configure("failed", foreground="#D32F2F")
        self.card_queue_tree.tag_configure("processing", foreground="#1976D2")

        # Card action queue data
        self._card_queue = []  # list of StagedCardAction

        # Resolved single user (stored for staging)
        self._single_resolved_user = None

        # ════════════════════════════════════════════════════════════════
        #  MIDDLE — Plan Selection & Action (fixed height)
        # ════════════════════════════════════════════════════════════════
        mid_frame = ttk.Frame(main_pane)
        main_pane.add(mid_frame, minsize=110, height=130, stretch="never")

        plan_frame = ttk.Labelframe(mid_frame, text="Plan Selection & Action",
                                    padding=(10, 5))
        plan_frame.pack(fill=tk.BOTH, expand=True)

        self.plan_notebook = ttk.Notebook(plan_frame)
        self.plan_notebook.pack(fill=tk.X, pady=(0, 4))

        # Door Access tab
        door_tab = ttk.Frame(self.plan_notebook, padding=8)
        self.plan_notebook.add(door_tab, text="Door Access Plans")

        ttk.Label(door_tab, text="Search:").pack(side=tk.LEFT, padx=(0, 4))
        self.door_filter_var = tk.StringVar()
        self.door_filter_var.trace_add("write", self._filter_door_plans)
        self.door_filter_entry = ttk.Entry(door_tab,
                                           textvariable=self.door_filter_var,
                                           width=20)
        self.door_filter_entry.pack(side=tk.LEFT, padx=(0, 8))

        ttk.Label(door_tab, text="Plan:").pack(side=tk.LEFT, padx=(0, 4))
        self.door_plan_var = tk.StringVar()
        self.door_plan_combo = ttk.Combobox(door_tab,
                                            textvariable=self.door_plan_var,
                                            state="readonly", width=45)
        self.door_plan_combo.pack(side=tk.LEFT, padx=(0, 16))

        self.door_action_var = tk.StringVar(value="Add")
        ttk.Radiobutton(door_tab, text="Add", variable=self.door_action_var,
                        value="Add").pack(side=tk.LEFT, padx=(0, 4))
        ttk.Radiobutton(door_tab, text="Remove", variable=self.door_action_var,
                        value="Remove").pack(side=tk.LEFT, padx=(0, 16))

        self.stage_door_btn = ttk.Button(door_tab, text="Stage Action",
                                         command=self._stage_door_action,
                                         style="Bold.TButton")
        self.stage_door_btn.pack(side=tk.LEFT)

        # Board/Meal tab
        board_tab = ttk.Frame(self.plan_notebook, padding=8)
        self.plan_notebook.add(board_tab, text="Board / Meal Plans")

        row_b1 = ttk.Frame(board_tab)
        row_b1.pack(fill=tk.X, pady=(0, 6))

        ttk.Label(row_b1, text="Search:").pack(side=tk.LEFT, padx=(0, 4))
        self.board_filter_var = tk.StringVar()
        self.board_filter_var.trace_add("write", self._filter_board_plans)
        board_filter_entry = ttk.Entry(row_b1,
                                       textvariable=self.board_filter_var,
                                       width=20)
        board_filter_entry.pack(side=tk.LEFT, padx=(0, 8))

        ttk.Label(row_b1, text="Plan:").pack(side=tk.LEFT, padx=(0, 4))
        self.board_plan_var = tk.StringVar()
        self.board_plan_combo = ttk.Combobox(row_b1,
                                             textvariable=self.board_plan_var,
                                             state="readonly", width=45)
        self.board_plan_combo.pack(side=tk.LEFT, padx=(0, 16))

        self.board_action_var = tk.StringVar(value="Add")
        ttk.Radiobutton(row_b1, text="Add", variable=self.board_action_var,
                        value="Add").pack(side=tk.LEFT, padx=(0, 4))
        ttk.Radiobutton(row_b1, text="Remove", variable=self.board_action_var,
                        value="Remove").pack(side=tk.LEFT, padx=(0, 16))

        self.stage_board_btn = ttk.Button(row_b1, text="Stage Action",
                                          command=self._stage_board_action,
                                          style="Bold.TButton")
        self.stage_board_btn.pack(side=tk.LEFT)

        # Board-specific options (second row)
        row_b2 = ttk.Frame(board_tab)
        row_b2.pack(fill=tk.X)

        ttk.Label(row_b2, text="Priority:").pack(side=tk.LEFT, padx=(0, 4))
        self.priority_var = tk.IntVar(value=1)
        ttk.Spinbox(row_b2, from_=1, to=99, textvariable=self.priority_var,
                    width=4).pack(side=tk.LEFT, padx=(0, 16))

        self.board_active_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(row_b2, text="Active",
                        variable=self.board_active_var).pack(
            side=tk.LEFT, padx=(0, 16))

        ttk.Label(row_b2, text="Start Date:").pack(side=tk.LEFT, padx=(0, 4))
        self.start_date_entry = ttk.Entry(row_b2, width=20)
        self.start_date_entry.pack(side=tk.LEFT, padx=(0, 8))

        ttk.Label(row_b2, text="End Date:").pack(side=tk.LEFT, padx=(0, 4))
        self.end_date_entry = ttk.Entry(row_b2, width=20)
        self.end_date_entry.pack(side=tk.LEFT, padx=(0, 8))

        ttk.Label(row_b2, text="(yyyy-mm-ddThh:mm:ss)",
                  foreground="gray").pack(side=tk.LEFT)

        # ════════════════════════════════════════════════════════════════
        #  BOTTOM PANE — Staged Operations (resizable)
        # ════════════════════════════════════════════════════════════════
        bot_pane_frame = ttk.Frame(main_pane)
        main_pane.add(bot_pane_frame, minsize=120, height=130, stretch="always")

        stage_frame = ttk.Labelframe(bot_pane_frame, text="Staged Operations",
                                     padding=(10, 5))
        stage_frame.pack(fill=tk.BOTH, expand=True)

        # Toolbar
        toolbar = ttk.Frame(stage_frame)
        toolbar.pack(fill=tk.X, pady=(0, 4))

        self.stage_count_var = tk.StringVar(value="0 operations staged")
        ttk.Label(toolbar, textvariable=self.stage_count_var).pack(side=tk.LEFT)

        ttk.Button(toolbar, text="Clear All",
                   command=self._clear_staged).pack(side=tk.RIGHT, padx=(4, 0))
        ttk.Button(toolbar, text="Remove Selected",
                   command=self._remove_staged_selected).pack(side=tk.RIGHT)

        ttk.Separator(toolbar, orient=tk.VERTICAL).pack(
            side=tk.RIGHT, fill=tk.Y, padx=6)
        ttk.Button(toolbar, text="Export Current State",
                   command=self._export_current_state).pack(
            side=tk.RIGHT, padx=(0, 4))
        ttk.Button(toolbar, text="Batch Compare",
                   command=self._batch_compare).pack(
            side=tk.RIGHT, padx=(0, 4))

        # Staging treeview
        stree_frame = ttk.Frame(stage_frame)
        stree_frame.pack(fill=tk.BOTH, expand=True)

        cols = ("num", "identifier", "name", "customer_num", "action",
                "plan_type", "plan_name", "current", "status")
        self.stage_tree = ttk.Treeview(stree_frame, columns=cols,
                                       show="headings", height=10)
        headings = ("#", "Identifier", "Name", "CustomerNumber", "Action",
                    "Plan Type", "Plan Name", "Current", "Status")
        widths = (40, 100, 140, 100, 50, 50, 160, 70, 160)
        for col, heading, w in zip(cols, headings, widths):
            self.stage_tree.heading(col, text=heading)
            self.stage_tree.column(col, width=w, minwidth=40)

        stage_vsb = ttk.Scrollbar(stree_frame, orient=tk.VERTICAL,
                                  command=self.stage_tree.yview)
        stage_hsb = ttk.Scrollbar(stree_frame, orient=tk.HORIZONTAL,
                                  command=self.stage_tree.xview)
        self.stage_tree.configure(yscrollcommand=stage_vsb.set,
                                  xscrollcommand=stage_hsb.set)
        self.stage_tree.grid(row=0, column=0, sticky="nsew")
        stage_vsb.grid(row=0, column=1, sticky="ns")
        stage_hsb.grid(row=1, column=0, sticky="ew")
        stree_frame.rowconfigure(0, weight=1)
        stree_frame.columnconfigure(0, weight=1)
        _treeview_sort_setup(self.stage_tree)

        # Tag colours
        # "pending" uses the default tree foreground so it adapts to
        # light/dark mode. Other states use distinct colors tuned to stay
        # readable on both light and dark backgrounds.
        self.stage_tree.tag_configure("processing", foreground="#1976D2")
        self.stage_tree.tag_configure("success", foreground="#228B22")
        self.stage_tree.tag_configure("failed", foreground="#D32F2F")
        self.stage_tree.tag_configure("already", foreground="#E67E22")

        # ── Bottom bar: progress + commit ───────────────────────────────
        bottom = ttk.Frame(self)
        bottom.pack(fill=tk.X, padx=10, pady=(4, 0))

        self.status_var = tk.StringVar(value="Ready.")
        ttk.Label(bottom, textvariable=self.status_var,
                  foreground="gray").pack(side=tk.LEFT, fill=tk.X, expand=True)

        self.progress_var = tk.DoubleVar(value=0)
        self.progress_bar = ttk.Progressbar(bottom, variable=self.progress_var,
                                            maximum=100, length=250)
        self.progress_bar.pack(side=tk.LEFT, padx=(8, 12))

        self.commit_btn = ttk.Button(bottom, text="Commit All",
                                     command=self._commit_actions,
                                     style="Bold.TButton")
        self.commit_btn.pack(side=tk.RIGHT)

        self.undo_btn = ttk.Button(bottom, text="Undo Last Commit",
                                   command=self._undo_last_commit,
                                   state=tk.DISABLED)
        self.undo_btn.pack(side=tk.RIGHT, padx=(0, 8))

        ttk.Button(bottom, text="Validate",
                   command=self._validate_staged).pack(side=tk.RIGHT, padx=(0, 8))

        self.retry_btn = ttk.Button(bottom, text="Retry Failed",
                                    command=self._retry_failed,
                                    state=tk.DISABLED)
        self.retry_btn.pack(side=tk.RIGHT, padx=(0, 8))

        ttk.Button(bottom, text="Export Failures",
                   command=self._export_failures).pack(side=tk.RIGHT, padx=(0, 8))

        # Track last commit for undo
        self._last_committed = []  # list of StagedActions that succeeded

        # ── Second bottom row: profiles + log toggle ────────────────────
        bottom2 = ttk.Frame(self)
        bottom2.pack(fill=tk.X, padx=10, pady=(2, 0))

        ttk.Label(bottom2, text="Profile:").pack(side=tk.LEFT, padx=(0, 4))
        self.profile_combo = ttk.Combobox(bottom2, state="readonly", width=20)
        self.profile_combo.pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(bottom2, text="Load",
                   command=self._load_profile).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(bottom2, text="Save",
                   command=self._save_profile).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(bottom2, text="Delete",
                   command=self._delete_profile).pack(side=tk.LEFT, padx=(0, 8))

        self._refresh_profile_list()

        self.log_toggle_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(bottom2, text="Show Log",
                        variable=self.log_toggle_var,
                        command=self._toggle_log_panel).pack(side=tk.RIGHT)

        # ── Log panel (hidden by default) ───────────────────────────────
        self._log_frame = ttk.Frame(self)
        # Not packed until toggled

        self.log_text = tk.Text(self._log_frame, height=6, wrap=tk.WORD,
                                font=("Courier", 9), state=tk.DISABLED)
        log_vsb = ttk.Scrollbar(self._log_frame, orient=tk.VERTICAL,
                                command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=log_vsb.set)
        self.log_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        log_vsb.pack(side=tk.RIGHT, fill=tk.Y)

        # ── Keyboard shortcuts ──────────────────────────────────────────
        self.bind("<Control-l>", lambda e: self._focus_single_lookup())
        self.bind("<Control-L>", lambda e: self._focus_single_lookup())
        self.bind("<Control-f>", lambda e: self._focus_plan_search())
        self.bind("<Control-F>", lambda e: self._focus_plan_search())

    # ════════════════════════════════════════════════════════════════════
    #  Startup
    # ════════════════════════════════════════════════════════════════════

    def _startup_sequence(self):
        if not self.cred_manager.has_all_credentials():
            self._open_settings()
        if self.cred_manager.has_all_credentials():
            self._connect_services()

    def _open_settings(self):
        t_creds = self.cred_manager.get_transact_creds()
        a_creds = self.cred_manager.get_ad_creds()
        dlg = TransactCredentialDialog(
            self, t_creds, a_creds,
            lookup_fields=self._lookup_fields,
            customer_number_attr=self._customer_attr)
        result = dlg.show()
        if not result:
            return
        tc = result["transact"]
        ac = result["ad"]
        self.cred_manager.store_transact_creds(
            tc["consumer_key"], tc["consumer_secret"], tc["hostname"],
            tc["route_scheme"], tc["route_value"])
        self.cred_manager.store_ad_creds(
            ac["server"], ac["username"], ac["password"], ac["use_ssl"])

        # Persist lookup-field config and apply to the UI
        self.cred_manager.set_lookup_fields(result["lookup_fields"])
        self.cred_manager.set_customer_number_attr(result["customer_number_attr"])
        self._lookup_fields = self.cred_manager.get_lookup_fields()
        self._customer_attr = self.cred_manager.get_customer_number_attr()
        self._apply_lookup_field_changes()

        self._connect_services()

    def _show_about(self):
        about = tk.Toplevel(self)
        about.title("About Transact Access Manager")
        about.geometry("560x700")
        about.minsize(400, 400)
        about.transient(self)
        about.grab_set()

        about.update_idletasks()
        x = self.winfo_x() + (self.winfo_width() // 2) - 280
        y = self.winfo_y() + (self.winfo_height() // 2) - 350
        about.geometry(f"+{x}+{y}")

        main = ttk.Frame(about, padding=20)
        main.pack(fill=tk.BOTH, expand=True)

        ttk.Label(main, text="Transact Access Manager",
                  font=("Helvetica", 18, "bold")).pack(pady=(0, 4))
        ttk.Label(main, text="v1.0",
                  font=("Helvetica", 11), foreground="gray").pack()

        ttk.Separator(main, orient=tk.HORIZONTAL).pack(fill=tk.X, pady=10)

        ttk.Label(main, text="Made by Jason Taylor",
                  font=("Helvetica", 12)).pack()
        ttk.Label(main, text="University of Colorado Colorado Springs",
                  font=("Helvetica", 11), foreground="gray").pack(pady=(2, 0))

        ttk.Separator(main, orient=tk.HORIZONTAL).pack(fill=tk.X, pady=10)

        poem = (
            "Do not go gentle into that good night,\n"
            "Old age should burn and rave at close of day;\n"
            "Rage, rage against the dying of the light.\n"
            "\n"
            "Though wise men at their end know dark is right,\n"
            "Because their words had forked no lightning they\n"
            "Do not go gentle into that good night.\n"
            "\n"
            "Good men, the last wave by, crying how bright\n"
            "Their frail deeds might have danced in a green bay,\n"
            "Rage, rage against the dying of the light.\n"
            "\n"
            "Wild men who caught and sang the sun in flight,\n"
            "And learn, too late, they grieved it on its way,\n"
            "Do not go gentle into that good night.\n"
            "\n"
            "Grave men, near death, who see with blinding sight\n"
            "Blind eyes could blaze like meteors and be gay,\n"
            "Rage, rage against the dying of the light.\n"
            "\n"
            "And you, my father, there on the sad height,\n"
            "Curse, bless, me now with your fierce tears, I pray.\n"
            "Do not go gentle into that good night.\n"
            "Rage, rage against the dying of the light."
        )

        bg_color = about.cget("bg")
        poem_text = tk.Text(main, wrap=tk.WORD, font=("Georgia", 13),
                            borderwidth=0, highlightthickness=0,
                            background=bg_color, cursor="arrow",
                            spacing1=2, spacing3=2)
        poem_text.insert("1.0", poem)
        poem_text.tag_configure("center", justify="center")
        poem_text.tag_add("center", "1.0", tk.END)
        poem_text.config(state=tk.DISABLED)
        poem_text.pack(fill=tk.BOTH, expand=True, pady=(0, 4))

        attrib_lbl = ttk.Label(main, text="-- Dylan Thomas",
                               font=("Georgia", 11, "italic"),
                               foreground="gray")
        attrib_lbl.pack(anchor=tk.E)

        ttk.Separator(main, orient=tk.HORIZONTAL).pack(fill=tk.X, pady=8)

        ttk.Button(main, text="Close",
                   command=about.destroy).pack()

        about.bind("<Escape>", lambda e: about.destroy())

        # Auto-scale poem font to fill available space
        _poem_lines = poem.count("\n") + 1
        _last_size = [0]  # mutable container to track last applied size

        def _rescale_poem(event=None):
            # Only respond to the toplevel window itself resizing
            if event and event.widget is not about:
                return
            avail = poem_text.winfo_height()
            if avail < 20:
                return
            ideal_size = max(9, min(22, int(avail / (_poem_lines * 1.6))))
            if ideal_size == _last_size[0]:
                return
            _last_size[0] = ideal_size
            poem_text.config(font=("Georgia", ideal_size))
            attrib_lbl.config(font=("Georgia", max(9, ideal_size - 2), "italic"))

        about.bind("<Configure>", _rescale_poem)
        about.after(100, _rescale_poem)

    def _connect_services(self):
        """Authenticate Transact OAuth + AD in a background thread."""
        self.transact_status_var.set("Transact: Connecting...")
        self.transact_status_lbl.config(bg="#666666")
        self.ad_status_var.set("AD: Connecting...")
        self.ad_status_lbl.config(bg="#666666")
        threading.Thread(target=self._connect_worker, daemon=True).start()

    def _connect_worker(self):
        errors = []

        # Transact OAuth
        tc = self.cred_manager.get_transact_creds()
        if tc:
            client = TransactOAuthClient(
                tc["consumer_key"], tc["consumer_secret"], tc["hostname"],
                tc["route_scheme"], tc["route_value"])
            ok, err = client.authenticate()
            if ok:
                self.api_client = client
            else:
                errors.append(f"Transact: {err}")

        # AD
        ac = self.cred_manager.get_ad_creds()
        if ac:
            ad = ADLookupClient(ac["server"], ac["username"], ac["password"],
                                ac["use_ssl"])
            ok, err = ad.connect()
            if ok:
                self.ad_client = ad
            else:
                errors.append(f"AD: {err}")

        # Fetch plans if Transact connected
        if self.api_client:
            dp, dp_err = self.api_client.get_door_access_plans()
            if dp_err:
                errors.append(f"Door plans: {dp_err}")
            self.door_plans = dp

            bp, bp_err = self.api_client.get_board_plans()
            if bp_err:
                errors.append(f"Board plans: {bp_err}")
            self.board_plans = bp

        # Update UI on main thread
        self.after(0, lambda: self._connect_done(errors))

    def _connect_done(self, errors):
        # Transact panel
        if self.api_client:
            dp = len(self.door_plans)
            bp = len(self.board_plans)
            self.transact_status_var.set(
                f"Transact: Connected ({dp} door plans, {bp} board plans)")
            self.transact_status_lbl.config(bg="#2d8a2d")
        else:
            t_err = next((e for e in errors if e.startswith("Transact:")), "")
            self.transact_status_var.set(
                f"Transact: Failed{' - ' + t_err if t_err else ''}")
            self.transact_status_lbl.config(bg="#cc3333")

        # AD panel
        if self.ad_client:
            self.ad_status_var.set("AD: Connected")
            self.ad_status_lbl.config(bg="#2d8a2d")
        else:
            a_err = next((e for e in errors if e.startswith("AD:")), "")
            self.ad_status_var.set(
                f"AD: Failed{' - ' + a_err if a_err else ''}")
            self.ad_status_lbl.config(bg="#cc3333")

        # Populate plan comboboxes
        self._populate_plan_combos()

        plan_summary = (f"{len(self.door_plans)} door plans, "
                        f"{len(self.board_plans)} board plans loaded")
        if errors:
            self._log(f"{plan_summary}. Warnings: {'; '.join(errors)}")
        else:
            self._log(f"{plan_summary}. Ready.")

    def _populate_plan_combos(self):
        door_names = self._build_door_plan_names()
        self.door_plan_combo["values"] = door_names
        self._door_plan_names_all = door_names

        board_names = self._build_board_plan_names()
        self.board_plan_combo["values"] = board_names
        self._board_plan_names_all = board_names

    def _build_door_plan_names(self):
        """Build display strings for door plans: 'id: name' or just 'id'."""
        names = []
        for p in self.door_plans:
            pid = p.get("id") or p.get("Id") or p.get("ID") or "?"
            pname = p.get("name") or p.get("Name") or ""
            active = p.get("active", p.get("Active", True))
            label = f"{pid}: {pname}" if pname else str(pid)
            if not active:
                label += " (inactive)"
            names.append(label)
        return names

    def _build_board_plan_names(self):
        names = []
        for p in self.board_plans:
            pid = p.get("Id") or p.get("id") or p.get("ID") or "?"
            pname = p.get("Name") or p.get("name") or ""
            active = p.get("Active", p.get("active", True))
            label = f"{pid}: {pname}" if pname else str(pid)
            if not active:
                label += " (inactive)"
            names.append(label)
        return names

    # ── Plan combo filtering (type-to-search) ───────────────────────────

    def _filter_door_plans(self, *_args):
        typed = self.door_filter_var.get().lower()
        if not typed:
            self.door_plan_combo["values"] = self._door_plan_names_all
        else:
            filtered = [n for n in self._door_plan_names_all
                        if typed in n.lower()]
            self.door_plan_combo["values"] = filtered
        # Auto-select first match
        vals = self.door_plan_combo["values"]
        if vals:
            self.door_plan_combo.current(0)
        else:
            self.door_plan_var.set("")

    def _filter_board_plans(self, *_args):
        typed = self.board_filter_var.get().lower()
        if not typed:
            self.board_plan_combo["values"] = self._board_plan_names_all
        else:
            filtered = [n for n in self._board_plan_names_all
                        if typed in n.lower()]
            self.board_plan_combo["values"] = filtered
        # Auto-select first match
        vals = self.board_plan_combo["values"]
        if vals:
            self.board_plan_combo.current(0)
        else:
            self.board_plan_var.set("")

    # ════════════════════════════════════════════════════════════════════
    #  File loading
    # ════════════════════════════════════════════════════════════════════

    def _browse_file(self):
        path = filedialog.askopenfilename(
            title="Select Input File",
            filetypes=(("Data Files", "*.csv *.txt"),
                       ("CSV Files", "*.csv"),
                       ("Text Files", "*.txt"),
                       ("All Files", "*.*")))
        if not path:
            return
        self.csv_path = path
        self.file_lbl_var.set(os.path.basename(path))
        self._load_file()

    def _detect_delimiter(self, filepath):
        try:
            with open(filepath, "r") as f:
                sample = f.read(8192)
            dialect = csv.Sniffer().sniff(sample, delimiters=",\t|;")
            return dialect.delimiter
        except csv.Error:
            return ","

    def _load_file(self):
        if not self.csv_path:
            return
        delim_choice = DELIMITER_MAP.get(self.delim_var.get())
        if delim_choice is None:
            delim_choice = self._detect_delimiter(self.csv_path)
        self.detected_delimiter = delim_choice

        with open(self.csv_path, "r", newline="") as f:
            reader = csv.reader(f, delimiter=self.detected_delimiter)
            self.csv_data = list(reader)

        if not self.csv_data:
            messagebox.showwarning("Empty File", "The selected file is empty.")
            return

        # Auto-detect header
        first = self.csv_data[0]
        numeric_count = sum(
            1 for c in first
            if c.strip().replace(".", "", 1).replace("-", "", 1).isdigit())
        has_header = numeric_count <= len(first) / 2
        self.has_header_var.set(has_header)
        self._apply_header_split()

    def _on_header_toggle(self):
        self._apply_header_split()

    def _apply_header_split(self):
        if not self.csv_data:
            return
        if self.has_header_var.get():
            self.csv_headers = self.csv_data[0]
            self.data_start_index = 1
        else:
            ncols = len(self.csv_data[0])
            self.csv_headers = [f"Column {i+1}" for i in range(ncols)]
            self.data_start_index = 0

        self.col_combo["values"] = self.csv_headers
        if self.csv_headers:
            self.col_combo.current(0)

        self.resolved_users = []
        self.resolve_status_var.set("")
        self._refresh_input_tree()

    def _refresh_input_tree(self):
        self.input_tree.delete(*self.input_tree.get_children())
        display_cols = list(self.csv_headers)
        # Add AD result columns if resolved
        if self.resolved_users:
            display_cols += ["AD Match", "Display Name", "Employee ID"]

        self.input_tree["columns"] = display_cols
        for col in display_cols:
            self.input_tree.heading(col, text=col)
            self.input_tree.column(col, width=110, minwidth=60)

        data_rows = self.csv_data[self.data_start_index:]
        for i, row in enumerate(data_rows[:200]):  # show up to 200
            padded = row + [""] * (len(self.csv_headers) - len(row))
            if self.resolved_users and i < len(self.resolved_users):
                ru = self.resolved_users[i]
                if ru:
                    padded += ["Yes", ru.display_name, ru.customer_number]
                else:
                    padded += ["No", "", ""]
            self.input_tree.insert("", tk.END, values=padded)

        total = len(data_rows)
        self.status_var.set(f"Loaded {total} data rows.")

    # ════════════════════════════════════════════════════════════════════
    #  AD Resolution
    # ════════════════════════════════════════════════════════════════════

    def _resolve_users(self):
        if not self.ad_client:
            messagebox.showwarning("Not Connected",
                                   "AD is not connected. Check Settings.")
            return
        col_name = self.col_combo.get()
        if not col_name or not self.csv_data:
            messagebox.showwarning("No Data",
                                   "Please load a file and select a column.")
            return

        col_index = self.csv_headers.index(col_name)
        ad_field_label = self.ad_field_combo.get()
        ad_field = self._attr_for_label(ad_field_label)
        if not ad_field:
            messagebox.showwarning("Config", "Please select an AD field.")
            return

        data_rows = self.csv_data[self.data_start_index:]
        identifiers = []
        for row in data_rows:
            val = row[col_index].strip() if col_index < len(row) else ""
            identifiers.append(val)

        self.resolve_btn.config(state=tk.DISABLED)
        self.resolve_status_var.set("Resolving...")
        self.progress_var.set(0)

        threading.Thread(
            target=self._resolve_worker,
            args=(identifiers, ad_field),
            daemon=True,
        ).start()

    def _resolve_worker(self, identifiers, ad_field):
        total = len(identifiers)

        def progress_cb(current, total_count):
            pct = (current / total_count) * 100
            self.after(0, lambda: self.progress_var.set(pct))
            if current % 10 == 0 or current == total_count:
                self.after(0, lambda c=current, t=total_count:
                           self.resolve_status_var.set(f"Resolving {c}/{t}..."))

        results = self.ad_client.lookup_batch(
            identifiers, ad_field,
            extra_attrs=self._all_lookup_attrs(),
            progress_callback=progress_cb)

        resolved = []
        match_count = 0
        for i, r in enumerate(results):
            if r:
                match_count += 1
                resolved.append(ResolvedUser(
                    identifier=identifiers[i],
                    display_name=r.get("displayName", "") or "",
                    customer_number=str(r.get(self._customer_attr, "") or ""),
                    email=r.get("mail", "") or "",
                    row_index=i,
                    extra=dict(r),
                ))
            else:
                resolved.append(None)

        self.resolved_users = resolved
        self.after(0, lambda: self._resolve_done(match_count, total))

    def _resolve_done(self, match_count, total):
        self.resolve_btn.config(state=tk.NORMAL)
        self.progress_var.set(100)
        self.resolve_status_var.set(
            f"Resolved {match_count}/{total} users")
        self._refresh_input_tree()
        self.status_var.set(
            f"AD resolution complete: {match_count} matched, "
            f"{total - match_count} unmatched.")

    # ════════════════════════════════════════════════════════════════════
    #  Single User Lookup
    # ════════════════════════════════════════════════════════════════════

    def _resolve_door_plan_name(self, plan_id):
        """Look up a door plan ID in the cached plan list and return its name."""
        for p in self.door_plans:
            pid = p.get("id") or p.get("Id") or p.get("ID")
            if str(pid) == str(plan_id):
                return p.get("name") or p.get("Name") or ""
        return ""

    def _resolve_board_plan_name(self, plan_id):
        """Look up a board plan ID in the cached plan list and return its name."""
        for p in self.board_plans:
            pid = p.get("Id") or p.get("id") or p.get("ID")
            if str(pid) == str(plan_id):
                return p.get("Name") or p.get("name") or ""
        return ""

    def _single_lookup(self):
        if not self.ad_client:
            messagebox.showwarning("Not Connected",
                                   "AD is not connected. Check Settings.")
            return
        search_val = self.single_search_var.get().strip()
        if not search_val:
            return

        ad_field_label = self.single_ad_field_combo.get()
        ad_field = self._attr_for_label(ad_field_label)
        if not ad_field:
            return

        self.single_lookup_btn.config(state=tk.DISABLED)
        self.single_status_var.set("Looking up...")
        self._single_resolved_user = None

        threading.Thread(
            target=self._single_lookup_worker,
            args=(search_val, ad_field),
            daemon=True,
        ).start()

    def _single_lookup_worker(self, search_val, ad_field):
        # AD lookup first — do this before Transact re-auth so the
        # LDAP connection doesn't go stale while waiting on HTTP calls
        # Use wildcard-aware multi lookup only when needed
        if "*" in search_val:
            results = self.ad_client.lookup_multi(
                search_val, ad_field, extra_attrs=self._all_lookup_attrs())
        else:
            single = self.ad_client.lookup(
                search_val, ad_field, extra_attrs=self._all_lookup_attrs())
            results = [single] if single else []

        if not results:
            self.after(0, lambda: self._single_lookup_done(
                search_val, None, [], [], [], None))
            return

        if len(results) == 1:
            self.after(0, lambda: self._single_finish_with_result(
                search_val, results[0]))
            return

        # Multiple matches — let the user pick on the main thread
        self.after(0, lambda: self._single_show_picker(search_val, results))

    def _single_show_picker(self, search_val, results):
        """Show a dialog to pick from multiple AD matches."""
        self.single_lookup_btn.config(state=tk.NORMAL)
        self.single_status_var.set(f"{len(results)} matches — pick one")

        picker = tk.Toplevel(self)
        picker.title(f"Multiple Matches for \"{search_val}\"")
        picker.geometry("500x350")
        picker.transient(self)
        picker.grab_set()
        picker.update_idletasks()
        x = self.winfo_x() + (self.winfo_width() // 2) - 250
        y = self.winfo_y() + (self.winfo_height() // 2) - 175
        picker.geometry(f"+{x}+{y}")

        ttk.Label(picker,
                  text=f"{len(results)} results — select one:",
                  padding=8).pack(fill=tk.X)

        cols = ("name", "custnum", "email")
        tree = ttk.Treeview(picker, columns=cols, show="headings", height=12)
        tree.heading("name", text="Name")
        tree.heading("custnum", text=self._customer_label())
        tree.heading("email", text="Email")
        tree.column("name", width=180, minwidth=80)
        tree.column("custnum", width=100, minwidth=60)
        tree.column("email", width=200, minwidth=80)

        vsb = ttk.Scrollbar(picker, orient=tk.VERTICAL, command=tree.yview)
        tree.configure(yscrollcommand=vsb.set)

        tree_frame = ttk.Frame(picker)
        tree_frame.pack(fill=tk.BOTH, expand=True, padx=8)
        tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)

        _treeview_sort_setup(tree)

        for i, r in enumerate(results):
            name = str(r.get("displayName", "") or "")
            custnum = str(r.get(self._customer_attr, "") or "")
            email = str(r.get("mail", "") or "")
            tree.insert("", tk.END, iid=str(i), values=(name, custnum, email))

        chosen = [None]

        def _on_select(event=None):
            sel = tree.selection()
            if sel:
                idx = int(sel[0])
                chosen[0] = results[idx]
                picker.destroy()

        def _on_cancel():
            picker.destroy()

        tree.bind("<Double-1>", _on_select)
        picker.bind("<Escape>", lambda e: _on_cancel())

        btn_row = ttk.Frame(picker, padding=8)
        btn_row.pack(fill=tk.X)
        ttk.Button(btn_row, text="Cancel", command=_on_cancel).pack(
            side=tk.RIGHT, padx=(4, 0))
        ttk.Button(btn_row, text="Select", command=_on_select).pack(
            side=tk.RIGHT)

        picker.wait_window()

        if chosen[0]:
            self.single_lookup_btn.config(state=tk.DISABLED)
            self.single_status_var.set("Loading Transact data...")
            threading.Thread(
                target=self._single_finish_with_result,
                args=(search_val, chosen[0]),
                daemon=True,
            ).start()
        else:
            self.single_status_var.set("")

    def _single_finish_with_result(self, search_val, result):
        """Given a single AD result, fetch Transact data and update UI."""
        door_plans = []
        board_plans = []
        cards = []
        mobile_cred = None
        if result and self.api_client:
            # Re-auth now, right before the Transact API calls
            self.api_client.reauthenticate()
            emp_id = str(result.get(self._customer_attr, "") or "")
            if emp_id:
                door_plans, _ = self.api_client.get_customer_door_plans(emp_id)
                board_plans, _ = self.api_client.get_customer_board_plans(emp_id)
                cards, _ = self.api_client.get_customer_cards(emp_id)

                cust, _ = self.api_client.get_customer(emp_id)
                if cust:
                    cust_guid = (cust.get("CustomerGuid")
                                 or cust.get("customerGuid") or "")
                    if cust_guid:
                        mobile_cred, _ = self.api_client.get_mobile_credential(
                            cust_guid)

        self.after(0, lambda: self._single_lookup_done(
            search_val, result, door_plans, board_plans, cards, mobile_cred))

    def _single_lookup_done(self, search_val, result, door_plans, board_plans,
                            cards=None, mobile_cred=None):
        self.single_lookup_btn.config(state=tk.NORMAL)

        # Clear previous
        for var in self._single_result_vars.values():
            var.set("")
        self.single_plans_tree.delete(*self.single_plans_tree.get_children())
        self._single_plan_ids = {}
        self.cards_tree.delete(*self.cards_tree.get_children())
        self._all_cards_raw = cards or []
        self._mobile_cred_raw = mobile_cred
        self.card_status_var.set("")
        self._single_resolved_user = None

        if not result:
            err = self.ad_client.last_error if self.ad_client else ""
            if err:
                self.single_status_var.set(f"Error: {err}")
            else:
                self.single_status_var.set("No match found.")
            return

        # Populate fields
        for attr, var in self._single_result_vars.items():
            var.set(str(result.get(attr, "") or ""))

        self.single_status_var.set("Found.")

        # Build resolved user for staging
        self._single_resolved_user = ResolvedUser(
            identifier=search_val,
            display_name=str(result.get("displayName", "") or ""),
            customer_number=str(result.get(self._customer_attr, "") or ""),
            email=str(result.get("mail", "") or ""),
            row_index=0,
            extra=dict(result),
        )

        # Populate current plans tree with resolved names
        # Store plan IDs keyed by tree iid for removal lookups
        self._single_plan_ids = {}

        for dp in door_plans:
            pid = dp if isinstance(dp, (int, str)) else (
                dp.get("id") or dp.get("Id") or "?")
            name = self._resolve_door_plan_name(pid)
            iid = self.single_plans_tree.insert(
                "", tk.END, values=("D", name or str(pid)))
            self._single_plan_ids[iid] = ("D", int(pid))
        for bp in board_plans:
            if isinstance(bp, dict):
                pid = (bp.get("boardPlanId") or bp.get("BoardPlanId")
                       or bp.get("id") or "?")
            else:
                pid = bp
            name = self._resolve_board_plan_name(pid)
            iid = self.single_plans_tree.insert(
                "", tk.END, values=("M", name or str(pid)))
            self._single_plan_ids[iid] = ("M", int(pid))

        # Populate cards tree (via shared method for filter support)
        self._populate_cards(cards)

    def _single_remove_plan(self):
        """Remove the selected plan from the current single user via API."""
        selected = self.single_plans_tree.selection()
        if not selected:
            messagebox.showinfo("No Selection",
                                "Select a plan to remove.")
            return
        if not self._single_resolved_user:
            return
        if not self.api_client:
            messagebox.showwarning("Not Connected",
                                   "Transact API is not connected.")
            return

        iid = selected[0]
        plan_info = getattr(self, '_single_plan_ids', {}).get(iid)
        if not plan_info:
            messagebox.showerror("Error", "Could not determine plan ID.")
            return
        plan_type, plan_id = plan_info
        item = self.single_plans_tree.item(iid)
        plan_name = item["values"][1] if len(item["values"]) > 1 else str(plan_id)

        emp_id = self._single_resolved_user.customer_number
        confirm = messagebox.askyesno(
            "Confirm Removal",
            f"Remove {'door' if plan_type == 'D' else 'meal'} plan "
            f"'{plan_name}' (ID {plan_id}) from "
            f"{self._single_resolved_user.display_name} ({emp_id})?")
        if not confirm:
            return

        self.single_status_var.set("Removing plan...")
        self.single_remove_btn.config(state=tk.DISABLED)
        threading.Thread(
            target=self._single_remove_worker,
            args=(emp_id, plan_type, plan_id, selected[0]),
            daemon=True,
        ).start()

    def _single_remove_worker(self, emp_id, plan_type, plan_id, tree_iid):
        # Fresh auth
        self.api_client.reauthenticate()

        if plan_type == "D":
            success, error_code, message = (
                self.api_client.remove_customer_door_plan(emp_id, plan_id))
        else:
            success, error_code, message = (
                self.api_client.remove_customer_board_plan(emp_id, plan_id))

        self.after(0, lambda: self._single_remove_done(
            success, message, tree_iid))

    def _single_remove_done(self, success, message, tree_iid):
        self.single_remove_btn.config(state=tk.NORMAL)
        if success:
            self.single_plans_tree.delete(tree_iid)
            self.single_status_var.set("Plan removed.")
        else:
            self.single_status_var.set(f"Remove failed: {message}")
            messagebox.showerror("Remove Failed", message)

    # ════════════════════════════════════════════════════════════════════
    #  Staging
    # ════════════════════════════════════════════════════════════════════

    def _get_resolved_user_list(self):
        """Return resolved users from whichever input tab is active."""
        active_tab = self.input_notebook.index(self.input_notebook.select())
        if active_tab == 1:  # Single User Lookup tab
            if self._single_resolved_user:
                return [self._single_resolved_user]
            return []
        # Bulk CSV tab
        return [u for u in self.resolved_users if u is not None]

    def _parse_plan_id(self, combo_value):
        """Extract numeric plan ID from combo display string like '3: Plan Name'."""
        try:
            return int(combo_value.split(":")[0].strip())
        except (ValueError, IndexError):
            return None

    def _stage_door_action(self):
        users = self._get_resolved_user_list()
        if not users:
            messagebox.showwarning("No Users",
                                   "No resolved users to stage. Use Bulk CSV "
                                   "or Single User Lookup first.")
            return

        plan_str = self.door_plan_var.get()
        plan_id = self._parse_plan_id(plan_str)
        if plan_id is None:
            messagebox.showwarning("No Plan",
                                   "Please select a door access plan.")
            return

        action = self.door_action_var.get()
        plan_name = plan_str

        for u in users:
            sa = StagedAction(
                user=u,
                action=action,
                plan_type="Door Access",
                plan_name=plan_name,
                plan_id=plan_id,
            )
            self.staged_actions.append(sa)

        self._refresh_stage_tree()
        self.status_var.set(
            f"Staged {len(users)} {action} operations for door plan '{plan_name}'.")

    def _stage_board_action(self):
        users = self._get_resolved_user_list()
        if not users:
            messagebox.showwarning("No Users",
                                   "No resolved users to stage. Use Bulk CSV "
                                   "or Single User Lookup first.")
            return

        plan_str = self.board_plan_var.get()
        plan_id = self._parse_plan_id(plan_str)
        if plan_id is None:
            messagebox.showwarning("No Plan",
                                   "Please select a board/meal plan.")
            return

        action = self.board_action_var.get()
        plan_name = plan_str
        priority = self.priority_var.get()
        active = self.board_active_var.get()
        start_date = self.start_date_entry.get().strip()
        end_date = self.end_date_entry.get().strip()

        for u in users:
            sa = StagedAction(
                user=u,
                action=action,
                plan_type="Board/Meal",
                plan_name=plan_name,
                plan_id=plan_id,
                priority=priority,
                active=active,
                start_date=start_date,
                end_date=end_date,
            )
            self.staged_actions.append(sa)

        self._refresh_stage_tree()
        self.status_var.set(
            f"Staged {len(users)} {action} operations for board plan '{plan_name}'.")

    def _refresh_stage_tree(self):
        self.stage_tree.delete(*self.stage_tree.get_children())
        for i, sa in enumerate(self.staged_actions):
            tag = self._status_tag(sa.status)
            self.stage_tree.insert("", tk.END, iid=str(i), values=(
                i + 1,
                sa.user.identifier,
                sa.user.display_name,
                sa.user.customer_number,
                sa.action,
                sa.plan_type,
                sa.plan_name,
                sa.current_state,
                sa.status,
            ), tags=(tag,))

        self.stage_count_var.set(f"{len(self.staged_actions)} operations staged")

    def _status_tag(self, status):
        s = status.lower()
        if "success" in s:
            return "success"
        if "fail" in s:
            return "failed"
        if "already" in s or "not found" in s:
            return "already"
        if "processing" in s:
            return "processing"
        return "pending"

    def _remove_staged_selected(self):
        selected = self.stage_tree.selection()
        if not selected:
            return
        indices = sorted([int(s) for s in selected], reverse=True)
        for idx in indices:
            if 0 <= idx < len(self.staged_actions):
                self.staged_actions.pop(idx)
        self._refresh_stage_tree()

    def _clear_staged(self):
        if not self.staged_actions:
            return
        if messagebox.askyesno("Clear All",
                               "Remove all staged operations?"):
            self.staged_actions.clear()
            self._refresh_stage_tree()

    # ════════════════════════════════════════════════════════════════════
    #  Commit
    # ════════════════════════════════════════════════════════════════════

    def _commit_actions(self):
        pending = [sa for sa in self.staged_actions if sa.status == "Pending"]
        if not pending:
            messagebox.showinfo("Nothing to Commit",
                                "No pending operations to commit.")
            return
        if not self.api_client:
            messagebox.showwarning("Not Connected",
                                   "Transact API is not connected.")
            return

        add_count = sum(1 for sa in pending if sa.action == "Add")
        rem_count = sum(1 for sa in pending if sa.action == "Remove")
        msg = (f"Execute {len(pending)} operations?\n"
               f"  {add_count} additions, {rem_count} removals")
        if not messagebox.askyesno("Confirm Commit", msg):
            return

        self.commit_btn.config(state=tk.DISABLED)
        self.progress_var.set(0)
        self.status_var.set("Committing...")
        threading.Thread(target=self._commit_worker, daemon=True).start()

    def _commit_worker(self):
        pending_indices = [i for i, sa in enumerate(self.staged_actions)
                           if sa.status == "Pending"]
        total = len(pending_indices)

        # Fresh OAuth handshake to ensure valid tokens
        self.after(0, lambda: self.status_var.set("Re-authenticating..."))
        ok, err = self.api_client.reauthenticate()
        if not ok:
            self.after(0, lambda: messagebox.showerror(
                "Auth Failed",
                f"Could not re-authenticate with Transact:\n{err}"))
            self.after(0, lambda: self.commit_btn.config(state=tk.NORMAL))
            return

        for count, idx in enumerate(pending_indices):
            sa = self.staged_actions[idx]
            self.after(0, lambda i=idx: self._set_row_status(i, "Processing..."))

            success, error_code, message = self._execute_action(sa)

            if success:
                sa.status = "Success"
                sa.error_message = ""
            elif error_code in (2209,):
                # Plan already assigned
                sa.status = "Already Assigned"
                sa.error_message = message
            elif error_code in (2210,):
                # Plan not found for removal
                sa.status = "Not Found"
                sa.error_message = message
            else:
                sa.status = f"Failed"
                sa.error_message = message

            self.after(0, lambda i=idx, s=sa.status, m=sa.error_message:
                       self._set_row_status(i, s if not m or "Success" in s
                                            else f"{s}: {m}"))

            pct = ((count + 1) / total) * 100
            self.after(0, lambda p=pct, c=count+1, t=total:
                       self._update_commit_progress(p, c, t))

        self.after(0, self._commit_complete)

    def _execute_action(self, sa):
        """Execute a single staged action. Returns (success, error_code, message)."""
        cn = sa.user.customer_number
        if sa.plan_type == "Door Access":
            if sa.action == "Add":
                return self.api_client.add_customer_door_plan(cn, sa.plan_id)
            else:
                return self.api_client.remove_customer_door_plan(cn, sa.plan_id)
        else:  # Board/Meal
            if sa.action == "Add":
                return self.api_client.add_customer_board_plan(
                    cn, sa.plan_id,
                    priority=sa.priority,
                    active=sa.active,
                    start_date=sa.start_date or None,
                    end_date=sa.end_date or None,
                )
            else:
                return self.api_client.remove_customer_board_plan(cn, sa.plan_id)

    def _set_row_status(self, idx, display_status):
        iid = str(idx)
        if self.stage_tree.exists(iid):
            sa = self.staged_actions[idx]
            tag = self._status_tag(display_status)
            self.stage_tree.item(iid, values=(
                idx + 1,
                sa.user.identifier,
                sa.user.display_name,
                sa.user.customer_number,
                sa.action,
                sa.plan_type,
                sa.plan_name,
                sa.current_state,
                display_status,
            ), tags=(tag,))
            self.stage_tree.see(iid)

    def _update_commit_progress(self, pct, current, total):
        self.progress_var.set(pct)
        self.status_var.set(f"Committed {current}/{total}...")

    def _commit_complete(self):
        self.commit_btn.config(state=tk.NORMAL)
        self.progress_var.set(100)

        succeeded = [sa for sa in self.staged_actions if sa.status == "Success"]
        already = sum(1 for sa in self.staged_actions
                      if sa.status in ("Already Assigned", "Not Found"))
        failed = sum(1 for sa in self.staged_actions if "Fail" in sa.status)
        pending = sum(1 for sa in self.staged_actions if sa.status == "Pending")

        # Store for undo
        self._last_committed = succeeded
        if succeeded:
            self.undo_btn.config(state=tk.NORMAL)

        # Enable retry if there were failures
        if failed > 0:
            self.retry_btn.config(state=tk.NORMAL)

        # Audit log
        self._audit_commit_results()

        msg = (f"Done: {len(succeeded)} success, {already} skipped, "
               f"{failed} failed.")
        self._log(msg)

        summary = (f"Commit complete.\n\n"
                   f"  Success: {len(succeeded)}\n"
                   f"  Already assigned / Not found: {already}\n"
                   f"  Failed: {failed}\n"
                   f"  Remaining pending: {pending}")
        messagebox.showinfo("Commit Results", summary)

        if failed > 0:
            if messagebox.askyesno("Export Failures",
                                   f"{failed} operations failed. Export to CSV?"):
                self._export_failures()

    # ════════════════════════════════════════════════════════════════════
    #  Export failures
    # ════════════════════════════════════════════════════════════════════

    def _export_failures(self):
        failures = [sa for sa in self.staged_actions if "Fail" in sa.status]
        if not failures:
            messagebox.showinfo("No Failures", "No failed operations to export.")
            return

        path = filedialog.asksaveasfilename(
            title="Export Failures",
            defaultextension=".csv",
            filetypes=(("CSV Files", "*.csv"), ("All Files", "*.*")))
        if not path:
            return

        with open(path, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["Identifier", "EmployeeID", "Action",
                             "Plan Type", "Plan Name", "Plan ID",
                             "Status", "Error"])
            for sa in failures:
                writer.writerow([
                    sa.user.identifier,
                    sa.user.customer_number,
                    sa.action,
                    sa.plan_type,
                    sa.plan_name,
                    sa.plan_id,
                    sa.status,
                    sa.error_message,
                ])

        self.status_var.set(f"Exported {len(failures)} failures to {os.path.basename(path)}")

    # ════════════════════════════════════════════════════════════════════
    #  Pre-commit validation + diff
    # ════════════════════════════════════════════════════════════════════

    def _validate_staged(self):
        """Check each staged user exists in Transact and show current plan state."""
        pending = [sa for sa in self.staged_actions if sa.status == "Pending"]
        if not pending:
            messagebox.showinfo("Nothing to Validate",
                                "No pending operations.")
            return
        if not self.api_client:
            messagebox.showwarning("Not Connected",
                                   "Transact API is not connected.")
            return

        self.commit_btn.config(state=tk.DISABLED)
        self.progress_var.set(0)
        self.status_var.set("Validating...")
        threading.Thread(target=self._validate_worker, daemon=True).start()

    def _validate_worker(self):
        # Fresh auth
        ok, err = self.api_client.reauthenticate()
        if not ok:
            self.after(0, lambda: messagebox.showerror(
                "Auth Failed", f"Could not authenticate:\n{err}"))
            self.after(0, lambda: self.commit_btn.config(state=tk.NORMAL))
            return

        pending_indices = [i for i, sa in enumerate(self.staged_actions)
                           if sa.status == "Pending"]
        total = len(pending_indices)

        # Cache lookups per employee_id to avoid redundant API calls
        plan_cache = {}  # emp_id -> {"door": set of ids, "board": set of ids}
        invalid_customers = set()
        valid_count = 0

        for count, idx in enumerate(pending_indices):
            sa = self.staged_actions[idx]
            emp_id = sa.user.customer_number

            self.after(0, lambda i=idx: self._set_row_status(i, "Validating..."))

            if emp_id not in plan_cache and emp_id not in invalid_customers:
                # Check customer exists
                cust, cust_err = self.api_client.get_customer(emp_id)
                if not cust:
                    invalid_customers.add(emp_id)
                else:
                    # Fetch current plans
                    door_plans, _ = self.api_client.get_customer_door_plans(emp_id)
                    board_plans, _ = self.api_client.get_customer_board_plans(emp_id)

                    door_ids = set()
                    for dp in door_plans:
                        pid = dp if isinstance(dp, (int, str)) else (
                            dp.get("id") or dp.get("Id") or 0)
                        door_ids.add(int(pid))

                    board_ids = set()
                    for bp in board_plans:
                        if isinstance(bp, dict):
                            pid = (bp.get("boardPlanId") or bp.get("BoardPlanId")
                                   or bp.get("id") or 0)
                        else:
                            pid = bp
                        board_ids.add(int(pid))

                    plan_cache[emp_id] = {"door": door_ids, "board": board_ids}

            # Set current state
            if emp_id in invalid_customers:
                sa.current_state = "NOT IN TRANSACT"
                sa.status = "Failed"
                sa.error_message = "Customer not found in Transact"
            else:
                cache = plan_cache[emp_id]
                if sa.plan_type == "Door Access":
                    has_plan = sa.plan_id in cache["door"]
                else:
                    has_plan = sa.plan_id in cache["board"]

                if has_plan:
                    sa.current_state = "Has plan"
                else:
                    sa.current_state = "No plan"
                valid_count += 1

            self.after(0, lambda i=idx, s=sa: self._set_row_status(
                i, s.status if s.status != "Pending" else "Pending"))

            pct = ((count + 1) / total) * 100
            self.after(0, lambda p=pct: self.progress_var.set(p))

        self.after(0, lambda: self._validate_done(
            valid_count, len(invalid_customers), total))

    def _validate_done(self, valid, invalid, total):
        self.commit_btn.config(state=tk.NORMAL)
        self.progress_var.set(100)
        self._refresh_stage_tree()

        msg = f"Validation complete: {valid} valid, {invalid} not in Transact"
        self.status_var.set(msg)

        if invalid > 0:
            messagebox.showwarning("Validation Issues",
                                   f"{invalid} user(s) not found in Transact.\n"
                                   f"Those rows have been marked as Failed.")

    # ════════════════════════════════════════════════════════════════════
    #  Undo last commit
    # ════════════════════════════════════════════════════════════════════

    def _undo_last_commit(self):
        if not self._last_committed:
            messagebox.showinfo("Nothing to Undo",
                                "No previous commit to undo.")
            return
        if not self.api_client:
            messagebox.showwarning("Not Connected",
                                   "Transact API is not connected.")
            return

        count = len(self._last_committed)
        adds = sum(1 for sa in self._last_committed if sa.action == "Add")
        removes = sum(1 for sa in self._last_committed if sa.action == "Remove")

        msg = (f"Undo {count} operations from last commit?\n\n"
               f"  {adds} additions will be REMOVED\n"
               f"  {removes} removals will be RE-ADDED\n\n"
               f"This cannot be undone.")
        if not messagebox.askyesno("Confirm Undo", msg):
            return

        # Create reversed staged actions
        undo_actions = []
        for sa in self._last_committed:
            reverse_action = "Remove" if sa.action == "Add" else "Add"
            undo = StagedAction(
                user=sa.user,
                action=reverse_action,
                plan_type=sa.plan_type,
                plan_name=sa.plan_name,
                plan_id=sa.plan_id,
                priority=sa.priority,
                active=sa.active,
                start_date=sa.start_date,
                end_date=sa.end_date,
            )
            undo_actions.append(undo)

        # Replace staged actions with undo operations and commit
        self.staged_actions = undo_actions
        self._last_committed = []
        self.undo_btn.config(state=tk.DISABLED)
        self._refresh_stage_tree()

        # Auto-commit the undo
        self.commit_btn.config(state=tk.DISABLED)
        self.progress_var.set(0)
        self.status_var.set("Undoing last commit...")
        threading.Thread(target=self._commit_worker, daemon=True).start()

    # ════════════════════════════════════════════════════════════════════
    #  Card management (single user tab)
    # ════════════════════════════════════════════════════════════════════

    def _card_on_double_click(self, event):
        """Handle double-click on the cards tree to show inline editor."""
        region = self.cards_tree.identify("region", event.x, event.y)
        if region != "cell":
            return
        col_id = self.cards_tree.identify_column(event.x)  # e.g. "#4"
        col_idx = int(col_id.replace("#", "")) - 1
        col_name = self.cards_tree["columns"][col_idx]

        is_combo = col_name in self._card_combo_cols
        is_spin = col_name in self._card_spin_cols
        if not is_combo and not is_spin:
            return

        iid = self.cards_tree.identify_row(event.y)
        if not iid:
            return

        bbox = self.cards_tree.bbox(iid, col_id)
        if not bbox:
            return
        x, y, w, h = bbox

        current_values = self.cards_tree.item(iid, "values")
        current_val = str(current_values[col_idx])

        def _apply(new_val):
            if new_val != current_val:
                vals = list(current_values)
                vals[col_idx] = new_val
                self.cards_tree.item(iid, values=vals)
                self._card_check_modified(iid)

        if is_combo:
            options = self._card_combo_cols[col_name]
            combo = ttk.Combobox(self.cards_tree, values=options,
                                 state="readonly", width=max(8, w // 8))
            combo.set(current_val)
            combo.place(x=x, y=y, width=w, height=h)
            combo.focus_set()
            combo.event_generate("<Button-1>")

            def _on_combo_select(e=None):
                _apply(combo.get())
                combo.destroy()

            combo.bind("<<ComboboxSelected>>", _on_combo_select)
            combo.bind("<Escape>", lambda e: combo.destroy())
            combo.bind("<FocusOut>", lambda e: combo.destroy())

        else:  # spinbox
            try:
                cur_int = int(current_val)
            except (ValueError, TypeError):
                cur_int = 1
            spin_var = tk.IntVar(value=cur_int)
            spin = ttk.Spinbox(self.cards_tree, from_=0, to=99,
                               textvariable=spin_var, width=4)
            spin.place(x=x, y=y, width=w, height=h)
            spin.focus_set()
            spin.selection_range(0, tk.END)

            def _on_spin_confirm(e=None):
                # Zero-pad to 2 digits to match Transact format
                new_val = f"{spin_var.get():02d}"
                spin.destroy()
                _apply(new_val)

            spin.bind("<Return>", _on_spin_confirm)
            spin.bind("<FocusOut>", _on_spin_confirm)
            spin.bind("<Escape>", lambda e: spin.destroy())

    def _card_check_modified(self, iid):
        """Compare current values to original and tag row if changed."""
        current = tuple(str(v) for v in self.cards_tree.item(iid, "values"))
        original = self._card_original_values.get(iid)
        if original and current != original:
            self.cards_tree.item(iid, tags=("modified",))
        else:
            self.cards_tree.item(iid, tags=())

        # Update status text with change count
        changed = sum(
            1 for i in self.cards_tree.get_children("")
            if i in self._card_original_values
            and tuple(str(v) for v in self.cards_tree.item(i, "values"))
            != self._card_original_values[i]
        )
        if changed:
            self.card_status_var.set(
                f"{changed} card(s) edited — click 'Stage Card Changes'")
        else:
            self.card_status_var.set(
                "Double-click Status, Lost, or Iss# to edit")

    def _stage_card_changes(self):
        """Collect inline edits from the cards tree and add to the queue."""
        if not self._single_resolved_user:
            return

        emp_id = self._single_resolved_user.customer_number
        name = self._single_resolved_user.display_name
        reason = self.retire_reason_var.get().strip()

        # Columns: card_num(0), issue(1), type(2), status(3), primary(4), lost(5)
        count = 0
        for iid in self.cards_tree.get_children(""):
            current = tuple(str(v) for v in self.cards_tree.item(iid, "values"))
            original = self._card_original_values.get(iid)
            if not original or current == original:
                continue

            card_num = current[0]

            if current[3] != original[3]:  # status changed
                detail = f"{original[3]} -> {current[3]}"
                sa = StagedCardAction(
                    customer_number=emp_id, display_name=name,
                    action_type="Card Status", card_number=card_num,
                    detail=detail, old_value=original[3],
                    new_value=current[3],
                    reason=reason if current[3] == "RETIRED" else "")
                self._card_queue.append(sa)
                count += 1

            if current[5] != original[5]:  # lost changed
                detail = f"Lost: {original[5]} -> {current[5]}"
                sa = StagedCardAction(
                    customer_number=emp_id, display_name=name,
                    action_type="Card Lost", card_number=card_num,
                    detail=detail, old_value=original[5],
                    new_value=current[5])
                self._card_queue.append(sa)
                count += 1

            if current[1] != original[1]:  # issue changed
                detail = f"Issue: {original[1]} -> {current[1]}"
                sa = StagedCardAction(
                    customer_number=emp_id, display_name=name,
                    action_type="Card Issue", card_number=card_num,
                    detail=detail, old_value=original[1],
                    new_value=current[1])
                self._card_queue.append(sa)
                count += 1

        if count == 0:
            messagebox.showinfo("No Changes",
                                "No card edits to stage. "
                                "Double-click Status, Lost, or Iss# to edit.")
            return

        # Reset the tree to original values now that changes are staged
        self._apply_card_filter()
        self._refresh_card_queue_tree()
        self.card_status_var.set(f"Staged {count} card change(s).")

    def _stage_retire_mobile(self):
        """Find non-STANDARD cards and stage them for retirement."""
        if not self._single_resolved_user:
            messagebox.showwarning("No User", "Look up a user first.")
            return

        emp_id = self._single_resolved_user.customer_number
        name = self._single_resolved_user.display_name
        reason = self.retire_reason_var.get().strip()

        # Columns: card_num(0), issue(1), type(2), status(3), primary(4), lost(5)
        count = 0
        for row in self._all_cards_data:
            card_num, issue, ctype, status, primary, lost = row
            # Only retire non-standard, non-retired cards
            if ctype.upper() == "STANDARD":
                continue
            if status.upper() == "RETIRED":
                continue

            detail = f"{status} -> RETIRED ({ctype})"
            sa = StagedCardAction(
                customer_number=emp_id, display_name=name,
                action_type="Card Status", card_number=card_num,
                detail=detail, old_value=status,
                new_value="RETIRED",
                reason=reason or "Mobile ID retired")
            self._card_queue.append(sa)
            count += 1

        if count == 0:
            messagebox.showinfo("No Mobile IDs",
                                "No non-standard active cards found to retire.")
            return

        self._refresh_card_queue_tree()
        self.card_status_var.set(
            f"Staged {count} mobile/non-standard card(s) for retirement.")

    def _stage_customer_active(self, active):
        """Stage a customer activation/deactivation."""
        if not self._single_resolved_user:
            messagebox.showwarning("No User", "Look up a user first.")
            return

        emp_id = self._single_resolved_user.customer_number
        name = self._single_resolved_user.display_name
        action = "Activate" if active else "Deactivate"

        sa = StagedCardAction(
            customer_number=emp_id, display_name=name,
            action_type="Customer Active",
            detail=f"{action} customer",
            new_value=str(active))
        self._card_queue.append(sa)
        self._refresh_card_queue_tree()
        self.card_status_var.set(f"Staged: {action} {name}")

    def _refresh_card_queue_tree(self):
        self.card_queue_tree.delete(*self.card_queue_tree.get_children())
        for i, sa in enumerate(self._card_queue):
            tag = sa.status.lower().replace(" ", "_")
            if tag not in ("pending", "success", "failed", "processing"):
                tag = "pending"
            self.card_queue_tree.insert("", tk.END, iid=str(i), values=(
                sa.customer_number, sa.action_type, sa.card_number,
                sa.detail, sa.status), tags=(tag,))

        has_pending = any(sa.status == "Pending" for sa in self._card_queue)
        self.commit_card_queue_btn.config(
            state=tk.NORMAL if has_pending else tk.DISABLED)

    def _clear_card_queue(self):
        self._card_queue.clear()
        self._refresh_card_queue_tree()

    def _commit_card_queue(self):
        """Commit all pending card/customer actions."""
        pending = [sa for sa in self._card_queue if sa.status == "Pending"]
        if not pending:
            return
        if not self.api_client:
            messagebox.showwarning("Not Connected",
                                   "Transact API is not connected.")
            return

        summary = "\n".join(
            f"  {sa.action_type}: {sa.card_number or sa.customer_number} "
            f"- {sa.detail}"
            for sa in pending)
        if not messagebox.askyesno(
                "Confirm Card/Customer Changes",
                f"Commit {len(pending)} action(s)?\n\n{summary}"):
            return

        self.commit_card_queue_btn.config(state=tk.DISABLED)
        self.card_status_var.set("Committing card queue...")
        threading.Thread(
            target=self._commit_card_queue_worker, daemon=True).start()

    def _commit_card_queue_worker(self):
        self.api_client.reauthenticate()

        pending_indices = [i for i, sa in enumerate(self._card_queue)
                           if sa.status == "Pending"]
        refreshed_emp_ids = set()

        for idx in pending_indices:
            sa = self._card_queue[idx]
            sa.status = "Processing"
            self.after(0, lambda: self._refresh_card_queue_tree())

            success, error_code, message = False, None, ""

            if sa.action_type == "Card Status":
                comment = sa.reason if sa.new_value == "RETIRED" else None
                success, error_code, message = self.api_client.update_card_status(
                    sa.customer_number, sa.card_number,
                    status_type=sa.new_value,
                    comment=comment)

            elif sa.action_type == "Card Lost":
                lost_bool = sa.new_value == "True"
                # Need current status for PATCH
                cards, _ = self.api_client.get_customer_cards(sa.customer_number)
                cur_status = "ACTIVE"
                for c in cards:
                    cn = str(c.get("cardNumber") or c.get("CardNumber") or "")
                    if cn == sa.card_number:
                        cur_status = (c.get("CardStatusType")
                                      or c.get("cardStatusType") or "ACTIVE")
                        break
                success, error_code, message = self.api_client.update_card_status(
                    sa.customer_number, sa.card_number,
                    status_type=cur_status, lost=lost_bool)

            elif sa.action_type == "Card Issue":
                cards, _ = self.api_client.get_customer_cards(sa.customer_number)
                cur_status = "ACTIVE"
                for c in cards:
                    cn = str(c.get("cardNumber") or c.get("CardNumber") or "")
                    if cn == sa.card_number:
                        cur_status = (c.get("CardStatusType")
                                      or c.get("cardStatusType") or "ACTIVE")
                        break
                success, error_code, message = self.api_client.update_card_status(
                    sa.customer_number, sa.card_number,
                    status_type=cur_status, issue_number=sa.new_value)

            elif sa.action_type == "Customer Active":
                active_bool = sa.new_value == "True"
                success, error_code, message = self.api_client.update_customer(
                    sa.customer_number, active=active_bool)

            sa.status = "Success" if success else "Failed"
            sa.error_message = "" if success else message
            refreshed_emp_ids.add(sa.customer_number)

            self._audit_log("CARD_QUEUE",
                            f"{sa.action_type}\t{sa.customer_number}\t"
                            f"{sa.card_number}\t{sa.detail}\t{sa.status}"
                            f"\t{sa.error_message}")

        self.after(0, lambda: self._commit_card_queue_done(refreshed_emp_ids))

    def _commit_card_queue_done(self, refreshed_emp_ids):
        self._refresh_card_queue_tree()

        success_count = sum(1 for sa in self._card_queue if sa.status == "Success")
        fail_count = sum(1 for sa in self._card_queue if sa.status == "Failed")

        self._log(f"Card queue: {success_count} OK, {fail_count} failed.")

        if fail_count:
            failures = "\n".join(
                f"  {sa.action_type} {sa.card_number}: {sa.error_message}"
                for sa in self._card_queue if sa.status == "Failed")
            messagebox.showwarning(
                "Card Queue Results",
                f"{success_count} succeeded, {fail_count} failed:\n\n{failures}")

        # Refresh cards for the looked-up user if applicable
        if (self._single_resolved_user and
                self._single_resolved_user.customer_number in refreshed_emp_ids):
            self._refresh_cards(self._single_resolved_user.customer_number)

    def _refresh_cards(self, emp_id):
        """Re-fetch and display cards for the given employee."""
        def worker():
            cards, _ = self.api_client.get_customer_cards(emp_id)
            self.after(0, lambda: self._populate_cards(cards))

        threading.Thread(target=worker, daemon=True).start()

    def _populate_cards(self, cards):
        # Store raw for JSON view
        self._all_cards_raw = cards or []
        # Parse into flat tuples for table view
        self._all_cards_data = []
        for card in (cards or []):
            if isinstance(card, dict):
                cnum = (card.get("cardNumber") or card.get("CardNumber")
                        or card.get("cardNum") or "?")
                issue = (card.get("issueNumber") or card.get("IssueNumber")
                         or card.get("Issue_Number") or "")
                ctype = (card.get("cardType") or card.get("CardType") or "")
                cstatus = (card.get("CardStatusType")
                           or card.get("cardStatusType") or "")
                primary = card.get("primary", card.get("Primary", ""))
                lost = card.get("lost", card.get("Lost", ""))
                self._all_cards_data.append(
                    (str(cnum), str(issue), str(ctype), str(cstatus),
                     str(primary), str(lost)))
        self._apply_card_filter()
        # Refresh raw view if it's currently shown
        if self.card_view_var.get() == "raw":
            self._refresh_raw_view()

    def _apply_card_filter(self):
        """Repopulate the cards tree, optionally hiding retired cards."""
        self.cards_tree.delete(*self.cards_tree.get_children())
        self._card_original_values = {}
        hide_retired = self.hide_retired_var.get()
        # Status is at index 3 (card_num, issue, type, status, primary, lost)
        for row in self._all_cards_data:
            if hide_retired and row[3].upper() == "RETIRED":
                continue
            iid = self.cards_tree.insert("", tk.END, values=row)
            self._card_original_values[iid] = row
        self.card_status_var.set("Double-click Status, Lost, or Iss# to edit")

    def _toggle_card_view(self):
        """Switch between table view and raw JSON view."""
        mode = self.card_view_var.get()
        if mode == "raw":
            self._cards_table_frame.pack_forget()
            self._cards_raw_frame.pack(fill=tk.BOTH, expand=True,
                                       in_=self._cards_view_container)
            self._refresh_raw_view()
        else:
            self._cards_raw_frame.pack_forget()
            self._cards_table_frame.pack(fill=tk.BOTH, expand=True,
                                         in_=self._cards_view_container)

    def _refresh_raw_view(self):
        """Update the raw JSON text widget with current card + mobile data."""
        import json as _json
        raw = {}
        if self._all_cards_raw:
            raw["cards"] = self._all_cards_raw
        if self._mobile_cred_raw:
            raw["mobile_credential"] = self._mobile_cred_raw
        if not raw:
            text = "(No card data available)"
        else:
            text = _json.dumps(raw, indent=2, default=str)
        self.cards_raw_text.config(state=tk.NORMAL)
        self.cards_raw_text.delete("1.0", tk.END)
        self.cards_raw_text.insert("1.0", text)
        self.cards_raw_text.config(state=tk.DISABLED)


    # ════════════════════════════════════════════════════════════════════
    #  Session keepalive
    # ════════════════════════════════════════════════════════════════════

    _KEEPALIVE_INTERVAL_MS = 120_000  # 2 minutes

    def _start_keepalive(self):
        """Schedule periodic keepalive pings."""
        self._keepalive_tick()

    def _keepalive_tick(self):
        threading.Thread(target=self._keepalive_worker, daemon=True).start()
        self.after(self._KEEPALIVE_INTERVAL_MS, self._keepalive_tick)

    def _keepalive_worker(self):
        # Ping AD — a lightweight search that keeps the connection alive
        if self.ad_client and self.ad_client._conn:
            try:
                self.ad_client._conn.search(
                    self.ad_client.base_dn, "(cn=keepalive-ping)",
                    attributes=["cn"], size_limit=1)
            except Exception:
                pass

    # ════════════════════════════════════════════════════════════════════
    #  Audit log
    # ════════════════════════════════════════════════════════════════════

    def _audit_log(self, action_type, details):
        """Append a line to the audit log file."""
        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"{ts}\t{action_type}\t{details}\n"
        try:
            os.makedirs(os.path.dirname(self._audit_log_path), exist_ok=True)
            with open(self._audit_log_path, "a") as f:
                f.write(line)
        except Exception:
            pass

    def _audit_commit_results(self):
        """Write all staged action results to audit log."""
        for sa in self.staged_actions:
            if sa.status in ("Pending",):
                continue
            self._audit_log("COMMIT", (
                f"{sa.action}\t{sa.plan_type}\t{sa.plan_name}\t"
                f"{sa.user.identifier}\t{sa.user.customer_number}\t"
                f"{sa.status}\t{sa.error_message}"))

    def _audit_card_change(self, card_num, field, old_val, new_val, success):
        """Log a card status change."""
        emp_id = (self._single_resolved_user.customer_number
                  if self._single_resolved_user else "?")
        self._audit_log("CARD", (
            f"{card_num}\t{emp_id}\t{field}: {old_val}->{new_val}\t"
            f"{'OK' if success else 'FAIL'}"))

    # ════════════════════════════════════════════════════════════════════
    #  Session log (status history)
    # ════════════════════════════════════════════════════════════════════

    def _log(self, message):
        """Add a timestamped message to the session log and status bar."""
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        line = f"[{ts}] {message}"
        self._log_lines.append(line)
        self.status_var.set(message)

        # Update the log text widget if visible
        if self.log_toggle_var.get():
            self.log_text.config(state=tk.NORMAL)
            self.log_text.insert(tk.END, line + "\n")
            self.log_text.see(tk.END)
            self.log_text.config(state=tk.DISABLED)

    def _toggle_log_panel(self):
        if self.log_toggle_var.get():
            self._log_frame.pack(fill=tk.X, padx=10, pady=(0, 4))
            # Populate with existing log lines
            self.log_text.config(state=tk.NORMAL)
            self.log_text.delete("1.0", tk.END)
            self.log_text.insert("1.0", "\n".join(self._log_lines) + "\n"
                                 if self._log_lines else "")
            self.log_text.see(tk.END)
            self.log_text.config(state=tk.DISABLED)
        else:
            self._log_frame.pack_forget()

    # ════════════════════════════════════════════════════════════════════
    #  Retry failed rows
    # ════════════════════════════════════════════════════════════════════

    def _retry_failed(self):
        """Re-stage all failed rows as pending."""
        failed = [sa for sa in self.staged_actions if "Fail" in sa.status]
        if not failed:
            messagebox.showinfo("No Failures", "No failed operations to retry.")
            return

        for sa in failed:
            sa.status = "Pending"
            sa.error_message = ""
            sa.current_state = ""

        self.retry_btn.config(state=tk.DISABLED)
        self._refresh_stage_tree()
        self._log(f"Re-staged {len(failed)} failed operations for retry.")

    # ════════════════════════════════════════════════════════════════════
    #  Keyboard shortcuts
    # ════════════════════════════════════════════════════════════════════

    def _focus_single_lookup(self):
        """Ctrl+L: switch to single user tab and focus search entry."""
        self.input_notebook.select(1)
        self.single_search_entry.focus_set()
        self.single_search_entry.selection_range(0, tk.END)

    def _focus_plan_search(self):
        """Ctrl+F: focus the plan search entry on the active plan tab."""
        active_tab = self.plan_notebook.index(self.plan_notebook.select())
        if active_tab == 0:
            self.door_filter_entry.focus_set()
        else:
            # Find the board filter entry
            try:
                self.board_filter_var.set("")
                # The board filter entry widget — find it by variable
                for child in self.plan_notebook.winfo_children():
                    for w in child.winfo_children():
                        if hasattr(w, 'cget') and isinstance(w, ttk.Entry):
                            w.focus_set()
                            return
            except Exception:
                pass

    # ════════════════════════════════════════════════════════════════════
    #  Settings persistence
    # ════════════════════════════════════════════════════════════════════

    def _load_saved_settings(self):
        """Restore UI state from settings.json."""
        settings = self.cred_manager.load_settings()
        if not settings:
            return

        geo = settings.get("window_geometry")
        if geo:
            try:
                self.geometry(geo)
            except Exception:
                pass

        # Saved values may be labels (legacy) or AD attributes (current).
        # Try both: attribute first, then label.
        self._select_lookup_combo(self.ad_field_combo,
                                  settings.get("ad_field_bulk_attr")
                                  or settings.get("ad_field_bulk"))
        self._select_lookup_combo(self.single_ad_field_combo,
                                  settings.get("ad_field_single_attr")
                                  or settings.get("ad_field_single"))

        plan_tab = settings.get("plan_tab")
        if plan_tab is not None:
            try:
                self.plan_notebook.select(plan_tab)
            except Exception:
                pass

        hide_retired = settings.get("hide_retired")
        if hide_retired is not None:
            self.hide_retired_var.set(hide_retired)

    def _save_current_settings(self):
        """Persist current UI state to settings.json."""
        settings = self.cred_manager.load_settings()
        settings["window_geometry"] = self.geometry()
        # Store by attribute so renaming labels doesn't break persistence.
        settings["ad_field_bulk_attr"] = (
            self._attr_for_label(self.ad_field_combo.get()) or "")
        settings["ad_field_single_attr"] = (
            self._attr_for_label(self.single_ad_field_combo.get()) or "")
        # Drop legacy label-based keys
        settings.pop("ad_field_bulk", None)
        settings.pop("ad_field_single", None)
        try:
            settings["plan_tab"] = self.plan_notebook.index(
                self.plan_notebook.select())
        except Exception:
            pass
        settings["hide_retired"] = self.hide_retired_var.get()
        self.cred_manager.save_settings(settings)

    def _select_lookup_combo(self, combo, value):
        """Select a lookup-field combo entry by attr name or legacy label."""
        if not value:
            return
        labels = self._lookup_labels()
        # Match by attr
        label = self._label_for_attr(value)
        if label and label in labels:
            combo.set(label)
            return
        # Match by legacy label
        if value in labels:
            combo.set(value)

    def destroy(self):
        """Override destroy to save settings on exit."""
        try:
            self._save_current_settings()
        except Exception:
            pass
        super().destroy()

    # ════════════════════════════════════════════════════════════════════
    #  Profiles / presets
    # ════════════════════════════════════════════════════════════════════

    _PROFILES_DIR = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        ".transact_access_configs", "profiles")

    def _refresh_profile_list(self):
        if not os.path.isdir(self._PROFILES_DIR):
            self.profile_combo["values"] = []
            return
        profiles = sorted(
            f.replace(".json", "")
            for f in os.listdir(self._PROFILES_DIR) if f.endswith(".json"))
        self.profile_combo["values"] = profiles

    def _save_profile(self):
        from tkinter import simpledialog
        name = simpledialog.askstring("Save Profile", "Profile name:",
                                      parent=self)
        if not name:
            return
        name = name.strip()
        os.makedirs(self._PROFILES_DIR, exist_ok=True)

        profile = {
            "plan_tab": self.plan_notebook.index(self.plan_notebook.select()),
            "door_plan": self.door_plan_var.get(),
            "door_action": self.door_action_var.get(),
            "board_plan": self.board_plan_var.get(),
            "board_action": self.board_action_var.get(),
            "priority": self.priority_var.get(),
            "board_active": self.board_active_var.get(),
            "start_date": self.start_date_entry.get(),
            "end_date": self.end_date_entry.get(),
            # Store by AD attribute so renaming labels doesn't break profiles
            "ad_field_bulk_attr": (
                self._attr_for_label(self.ad_field_combo.get()) or ""),
            "ad_field_single_attr": (
                self._attr_for_label(self.single_ad_field_combo.get()) or ""),
        }
        path = os.path.join(self._PROFILES_DIR, f"{name}.json")
        with open(path, "w") as f:
            json.dump(profile, f, indent=2)

        self._refresh_profile_list()
        self.profile_combo.set(name)
        self._log(f"Profile '{name}' saved.")

    def _load_profile(self):
        name = self.profile_combo.get()
        if not name:
            return
        path = os.path.join(self._PROFILES_DIR, f"{name}.json")
        if not os.path.isfile(path):
            messagebox.showwarning("Not Found", f"Profile '{name}' not found.")
            return

        with open(path, "r") as f:
            profile = json.load(f)

        try:
            self.plan_notebook.select(profile.get("plan_tab", 0))
        except Exception:
            pass

        door_plan = profile.get("door_plan", "")
        if door_plan:
            self.door_plan_var.set(door_plan)
        self.door_action_var.set(profile.get("door_action", "Add"))

        board_plan = profile.get("board_plan", "")
        if board_plan:
            self.board_plan_var.set(board_plan)
        self.board_action_var.set(profile.get("board_action", "Add"))

        self.priority_var.set(profile.get("priority", 1))
        self.board_active_var.set(profile.get("board_active", True))

        start = profile.get("start_date", "")
        self.start_date_entry.delete(0, tk.END)
        if start:
            self.start_date_entry.insert(0, start)

        end = profile.get("end_date", "")
        self.end_date_entry.delete(0, tk.END)
        if end:
            self.end_date_entry.insert(0, end)

        self._select_lookup_combo(
            self.ad_field_combo,
            profile.get("ad_field_bulk_attr") or profile.get("ad_field_bulk"))
        self._select_lookup_combo(
            self.single_ad_field_combo,
            profile.get("ad_field_single_attr")
            or profile.get("ad_field_single"))

        self._log(f"Profile '{name}' loaded.")

    def _delete_profile(self):
        name = self.profile_combo.get()
        if not name:
            return
        if not messagebox.askyesno("Delete Profile",
                                    f"Delete profile '{name}'?"):
            return
        path = os.path.join(self._PROFILES_DIR, f"{name}.json")
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        self.profile_combo.set("")
        self._refresh_profile_list()
        self._log(f"Profile '{name}' deleted.")

    # ════════════════════════════════════════════════════════════════════
    #  Batch compare (diff two CSVs)
    # ════════════════════════════════════════════════════════════════════

    def _batch_compare(self):
        """Load two CSVs and generate add/remove delta as staged actions."""
        old_path = filedialog.askopenfilename(
            title="Select OLD roster (previous semester)",
            filetypes=(("CSV Files", "*.csv *.txt"), ("All Files", "*.*")))
        if not old_path:
            return
        new_path = filedialog.askopenfilename(
            title="Select NEW roster (current semester)",
            filetypes=(("CSV Files", "*.csv *.txt"), ("All Files", "*.*")))
        if not new_path:
            return

        def read_ids(path):
            with open(path, "r", newline="") as f:
                reader = csv.reader(f)
                rows = list(reader)
            if not rows:
                return set()
            # Use first column, skip header if detected
            first = rows[0][0]
            start = 1 if not first.strip().replace(".", "").replace("-", "").isdigit() else 0
            return {row[0].strip() for row in rows[start:] if row and row[0].strip()}

        old_ids = read_ids(old_path)
        new_ids = read_ids(new_path)

        to_add = new_ids - old_ids
        to_remove = old_ids - new_ids
        unchanged = old_ids & new_ids

        msg = (f"Old roster: {len(old_ids)} users\n"
               f"New roster: {len(new_ids)} users\n\n"
               f"  To add: {len(to_add)}\n"
               f"  To remove: {len(to_remove)}\n"
               f"  Unchanged: {len(unchanged)}\n\n"
               f"Stage these as operations? (Select plan first)")

        if not messagebox.askyesno("Batch Compare Results", msg):
            return

        # Need a plan selected
        plan_str = self.door_plan_var.get() or self.board_plan_var.get()
        plan_id = self._parse_plan_id(plan_str)
        if plan_id is None:
            messagebox.showwarning("No Plan",
                                   "Please select a plan before running "
                                   "batch compare.")
            return

        active_tab = self.plan_notebook.index(self.plan_notebook.select())
        plan_type = "Door Access" if active_tab == 0 else "Board/Meal"

        # Create placeholder users (will need AD resolution)
        for ident in sorted(to_add):
            sa = StagedAction(
                user=ResolvedUser(ident, "", ident, "", 0),
                action="Add", plan_type=plan_type,
                plan_name=plan_str, plan_id=plan_id)
            self.staged_actions.append(sa)

        for ident in sorted(to_remove):
            sa = StagedAction(
                user=ResolvedUser(ident, "", ident, "", 0),
                action="Remove", plan_type=plan_type,
                plan_name=plan_str, plan_id=plan_id)
            self.staged_actions.append(sa)

        self._refresh_stage_tree()
        self._log(f"Batch compare: staged {len(to_add)} adds, "
                  f"{len(to_remove)} removes.")

    # ════════════════════════════════════════════════════════════════════
    #  Export current state
    # ════════════════════════════════════════════════════════════════════

    def _export_current_state(self):
        """For resolved users, export a report of their current plans."""
        users = self._get_resolved_user_list()
        if not users:
            messagebox.showwarning("No Users", "Resolve users first.")
            return
        if not self.api_client:
            messagebox.showwarning("Not Connected",
                                   "Transact API is not connected.")
            return

        path = filedialog.asksaveasfilename(
            title="Export Current State",
            defaultextension=".csv",
            filetypes=(("CSV Files", "*.csv"), ("All Files", "*.*")))
        if not path:
            return

        self._log("Exporting current state...")
        self.commit_btn.config(state=tk.DISABLED)
        self.progress_var.set(0)

        threading.Thread(
            target=self._export_state_worker,
            args=(users, path),
            daemon=True).start()

    def _export_state_worker(self, users, path):
        self.api_client.reauthenticate()

        rows = []
        total = len(users)
        for i, u in enumerate(users):
            emp_id = u.customer_number
            door_plans, _ = self.api_client.get_customer_door_plans(emp_id)
            board_plans, _ = self.api_client.get_customer_board_plans(emp_id)

            door_names = []
            for dp in door_plans:
                pid = dp if isinstance(dp, (int, str)) else (
                    dp.get("id") or dp.get("Id") or "?")
                name = self._resolve_door_plan_name(pid)
                door_names.append(name or str(pid))

            board_names = []
            for bp in board_plans:
                if isinstance(bp, dict):
                    pid = (bp.get("boardPlanId") or bp.get("BoardPlanId")
                           or bp.get("id") or "?")
                else:
                    pid = bp
                name = self._resolve_board_plan_name(pid)
                board_names.append(name or str(pid))

            rows.append([
                u.identifier, u.display_name, emp_id,
                "; ".join(door_names), "; ".join(board_names)])

            pct = ((i + 1) / total) * 100
            self.after(0, lambda p=pct: self.progress_var.set(p))

        # Write CSV
        with open(path, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["Identifier", "Name", "EmployeeID",
                             "Door Access Plans", "Board/Meal Plans"])
            writer.writerows(rows)

        self.after(0, lambda: self._export_state_done(path, len(rows)))

    def _export_state_done(self, path, count):
        self.commit_btn.config(state=tk.NORMAL)
        self.progress_var.set(100)
        self._log(f"Exported current state for {count} users to "
                  f"{os.path.basename(path)}")


# ── Entry point ─────────────────────────────────────────────────────────────

def main():
    app = TransactAccessManagerApp()
    app.mainloop()


if __name__ == "__main__":
    main()
