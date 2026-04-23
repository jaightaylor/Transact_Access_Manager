# Transact Access Manager (TAM)

A desktop GUI for managing door-access and board/meal-plan assignments in
**Transact Campus** (BBTS) via the official Management API. TAM replaces the
manual "export CSV from AD → import CSV into Transact" workflow with a direct,
audited, batched UI.

Built originally for the University of Colorado Colorado Springs, but written
to be institution-agnostic: the Active Directory attributes used for lookups —
including the one that maps to Transact `CustomerNumber` — are configurable
from the Settings dialog.

---

## Contents

- [Features](#features)
- [Requirements](#requirements)
- [Quick start](#quick-start)
- [First run — credentials](#first-run--credentials)
- [Configuring lookup fields](#configuring-lookup-fields)
- [Workflow](#workflow)
  - [Bulk (CSV)](#bulk-csv)
  - [Single user lookup](#single-user-lookup)
  - [Batch compare](#batch-compare)
- [Profiles](#profiles)
- [Audit log](#audit-log)
- [Keyboard shortcuts](#keyboard-shortcuts)
- [Project structure](#project-structure)
- [Building a standalone executable](#building-a-standalone-executable)
- [Configuration files](#configuration-files)
- [Troubleshooting](#troubleshooting)
- [Security notes](#security-notes)

---

## Features

- **Bulk import** from CSV or delimited text with auto-detected delimiters and
  header row.
- **Active Directory resolution** — resolve any configured AD attribute
  (student ID, employee ID, username, email, custom extension attributes) to a
  Transact `CustomerNumber`.
- **Configurable lookup fields** — add, rename, reorder, or remove AD search
  fields; designate any one of them as the source of `CustomerNumber`.
- **Door access plans** — stage and commit add/remove operations in bulk.
- **Board / meal plans** — stage and commit adds/removes with priority, active
  flag, and start/end dates.
- **Card management** — update card status (`ACTIVE` / `FROZEN` / `RETIRED`),
  mark cards lost/recovered, adjust issue numbers, retire mobile credentials,
  and toggle customer active state.
- **Single-user lookup** — search AD, review current plans and cards, and make
  point edits without building a CSV.
- **Batch compare** — diff a previous-semester roster against a current one and
  auto-stage the adds and removes needed to reconcile them.
- **Profiles** — save plan-selection presets (e.g., "Spring Housing — Dorm A")
  and recall them in one click.
- **Current-state export** — export a CSV of every current plan/card
  assignment for the resolved user set.
- **Audit log** — every committed action is appended to `audit.log` with
  timestamp, actor, target, action, and outcome.
- **Retry failed** — re-commit just the subset that errored, without re-staging.
- **Undo last commit** — reverse the most recent successful batch in one
  action.
- **Cross-platform** — macOS, Windows, Linux (native `pyad` on Windows,
  `ldap3` everywhere).
- **Secure credential storage** — OAuth and bind credentials are stored in the
  OS keychain (macOS Keychain, Windows Credential Manager, Linux
  SecretService), never on disk in plaintext.

## Requirements

- Python **3.10+**
- Runtime dependencies: `requests`, `requests-oauthlib`, `oauthlib`, `keyring`,
  `ldap3`
- Optional: `pyad` (Windows-only; uses the current domain session instead of
  an explicit bind)
- Transact Campus BBTS Management API access with an OAuth 1.0 consumer
  key/secret
- An AD/LDAP bind account with read access to the attributes you want to
  search by

## Quick start

### macOS / Linux

```bash
./run.sh
```

### Windows

Double-click `run.bat` or run it from a terminal.

Both scripts create a `venv/`, install the pinned dependencies, and launch the
GUI.

### Manual setup

```bash
python3 -m venv venv
source venv/bin/activate            # Windows: venv\Scripts\activate
pip install -r requirements_transact.txt
python transact_access_manager.py
```

---

## First run — credentials

On first launch TAM opens a **Settings** dialog with three tabs:

### 1. Transact API

| Field            | Description                                                |
|------------------|------------------------------------------------------------|
| Hostname         | Transact host, e.g. `yourschool.tsecloud.net`              |
| Consumer Key     | OAuth 1.0 consumer key issued by Transact                  |
| Consumer Secret  | OAuth 1.0 consumer secret                                  |
| Route Scheme     | Routing scheme header, usually `InstitutionRouteID`        |
| Route ID         | Institution routing value (ask Transact if unsure)         |

### 2. Active Directory

| Field            | Description                                                |
|------------------|------------------------------------------------------------|
| Server           | AD/LDAP server host, e.g. `ldap.yourschool.edu`            |
| Username         | Bind DN or `DOMAIN\user`                                   |
| Password         | Bind password                                              |
| Use SSL          | Use LDAPS (recommended — required for Windows AD writes)   |

### 3. Lookup Fields

Configure which AD attributes appear as "Search by" options and which one
maps to Transact `CustomerNumber`. **See the next section for details.**

All fields on all three tabs are saved together when you click **Save**.
Credentials go to the OS keychain; lookup-field config goes to
`.transact_access_configs/settings.json`.

---

## Configuring lookup fields

TAM ships with defaults that work for UCCS:

| Label            | AD Attribute           | Role                     |
|------------------|------------------------|--------------------------|
| CA8 Student ID   | `extensionAttribute8`  |                          |
| CA2 HR ID        | `extensionAttribute2`  |                          |
| UCCS ID          | `employeeID`           | ★ Transact CustomerNumber |
| Username         | `cn`                   |                          |
| Email            | `mail`                 |                          |

To adapt to a different institution, open **Settings → Lookup Fields**:

- **Add…** — create a new entry by supplying a user-facing **Label** and an
  **AD Attribute** (e.g. `sAMAccountName`, `uid`, `extensionAttribute5`).
- **Edit…** — rename a label or repoint it at a different AD attribute.
- **Remove** — delete an entry you don't use.
- **Move Up / Move Down** — control the order in the Search-by dropdowns.
- **Set as Customer #** — mark the selected row ★ to indicate that its AD
  attribute supplies Transact `CustomerNumber` for each resolved user.

The ★-marked field is what gets passed to every Transact API call (door
plans, board plans, cards, customer active/inactive). Changing which field is
★-marked takes effect immediately after Save — you do not need to restart.

### Practical examples

- **School A** stores the Transact identifier in AD's `employeeNumber`: add a
  lookup field with attribute `employeeNumber` and click *Set as Customer #*.
- **School B** uses `uid` for both login and Transact: keep a single lookup
  field called "Network ID" with attribute `uid`, star it as Customer #.
- **School C** has separate student and staff IDs in `extensionAttribute10`
  and `extensionAttribute11`: add both as searchable fields, but only one can
  be ★.

> **Saved references survive renames.** Internally, TAM stores
> `settings.json` and profile presets by *AD attribute*, not by label. You
> can rename "CA8 Student ID" to anything you like and existing profiles
> still load correctly.

---

## Workflow

### Bulk (CSV)

1. **Browse File…** to load a CSV / TSV / text file. Delimiter auto-detects;
   override with the Delimiter dropdown if needed.
2. Pick the **Identifier Column** (the column holding the values you want to
   look up) and the **AD Field** those values should be matched against.
3. Click **Resolve Users** — TAM queries AD in a background thread, shows a
   progress bar, and fills the preview with ✅ matches and ⚠ unmatched rows.
4. In the middle pane, pick a **Door Access Plan** or **Board / Meal Plan**,
   choose Add or Remove, fill in board-plan options if applicable, and click
   **Stage Action**. Staged actions accumulate in the bottom pane.
5. **Validate** to check for conflicts (e.g., removing a plan a user doesn't
   have); **Commit All** to run them through the API. Per-row status colors
   indicate success (green), failure (red), or already-in-state (orange).
6. If anything fails, fix it (re-stage, tweak credentials) and use **Retry
   Failed** to replay only the errored rows. Use **Undo Last Commit** to
   reverse the batch if needed.

### Single user lookup

The **Single User Lookup** tab is a point-and-click alternative to CSV work.
Search AD by any configured field, review the user's current door plans,
board plans, and physical/mobile cards, and make changes directly:

- Remove a plan by selecting it in the **Current Plans** list and clicking
  *Remove Selected*.
- Double-click a card row to edit its Status, Lost flag, or Issue number.
  Modified rows highlight yellow until you click **Stage Card Changes**.
- Toggle between **Table** and **Raw JSON** views of the API response for
  debugging.
- The AD Info panel is built from your configured lookup fields — whatever
  you added to Settings appears here too. The ★ field is the one used as
  `CustomerNumber`.

### Batch compare

Use **Batch Compare** (in the Staged Operations toolbar) to reconcile two
rosters:

1. Pick the **OLD** roster (e.g., last semester's CSV).
2. Pick the **NEW** roster.
3. Pick a plan (door or board).
4. TAM diffs the `CustomerNumber` columns and auto-stages:
   - **Remove** for users in OLD but not NEW
   - **Add** for users in NEW but not OLD

Review the staged actions and Commit as normal.

---

## Profiles

**Save** captures the current plan-selection state — plan tab, chosen plan,
action, board options, AD field selections. **Load** restores it. Profiles
are stored as JSON in `.transact_access_configs/profiles/` and can be
version-controlled or shared between machines.

## Audit log

Every committed action (including cards and single-user ops) appends one
tab-separated line to `.transact_access_configs/audit.log`:

```
2026-04-23T14:52:10   abc123   Jane Doe     ADD      Board/Meal Plan 17    OK
2026-04-23T14:52:11   abc124   John Smith   REMOVE   Door Plan 8           HTTP 404
```

The log is never rotated or truncated by TAM — back it up or rotate it
yourself if retention matters.

## Keyboard shortcuts

| Shortcut          | Action                                          |
|-------------------|-------------------------------------------------|
| `Ctrl` + `L`      | Focus the Single User Lookup search field       |
| `Ctrl` + `F`      | Focus the currently visible plan search filter  |
| `Enter` in Search | Run a lookup                                    |
| `Double-click`    | Edit a card row / pick a multiple-match result  |

## Project structure

```
transact_access_manager.py        Main GUI application (tkinter)
transact_api.py                   OAuth 1.0 HMAC-SHA1 Transact client
transact_credential_manager.py    OS-keychain credential store + settings.json
ad_lookup.py                      Cross-platform AD/LDAP lookup client
requirements_transact.txt         Runtime dependencies
transact_access_manager.spec      PyInstaller build spec
run.sh / run.bat                  Launcher scripts (venv + pip + run)
```

## Building a standalone executable

```bash
source venv/bin/activate
pip install pyinstaller
pyinstaller transact_access_manager.spec
```

Output:

- macOS → `dist/Transact Access Manager.app`
- Windows → `dist/Transact Access Manager.exe`
- Linux → `dist/Transact Access Manager/` (folder)

## Configuration files

All non-secret state lives under `.transact_access_configs/` next to the
script (or the executable):

| File                 | Contents                                                |
|----------------------|---------------------------------------------------------|
| `settings.json`      | Window geometry, last-selected AD fields, lookup-field definitions, customer-number attribute, Hide-Retired toggle |
| `profiles/*.json`    | Plan-selection presets                                  |
| `audit.log`          | Append-only record of committed API operations          |

Secrets (OAuth keys, AD password) are **never** stored here — they live in
the OS keychain under the service names
`transact-access-manager-transact` and `transact-access-manager-ad`.

## Troubleshooting

**"AD: Not connected" in red.**
Open Settings → Active Directory. Common causes: wrong server hostname,
firewall blocking port 636 (LDAPS) or 389 (LDAP), expired bind password, or a
bind DN that lacks read access to the attributes you need.

**`lookup returned 0 matches`.**
Verify the AD attribute you selected actually holds the value you're
searching for. `CustomerNumber == AD attribute` depends on how your
institution populates AD — it's not always `employeeID`.

**`HTTP 401 Unauthorized` from Transact.**
OAuth signatures are time-sensitive. Check system clock drift and confirm
the consumer key/secret pair matches the Hostname tenant.

**`HTTP 404` when committing a plan change.**
Either the user isn't in Transact (the AD `CustomerNumber` doesn't match any
Transact customer), or the plan ID no longer exists. The status column shows
the exact endpoint that failed for each row.

**Single-user pane feels cramped.**
The vertical split is drag-resizable. Defaults favor a large top pane for
the single-user cards view; middle and bottom panes can be shrunk with the
sashes.

**Windows: `pyad` errors on launch.**
TAM falls back to `ldap3` automatically. If you prefer `pyad` (no explicit
bind needed), make sure you're running in the same domain session as your
AD account and that `pywin32` is installed.

## Security notes

- OAuth secrets and AD bind passwords are written only to the OS keychain.
- The `keyring` library does not require an agent — on macOS you may be
  prompted to allow the binary to read/write its own Keychain entries the
  first time.
- `audit.log` is plaintext and contains usernames, Transact CustomerNumbers,
  and plan IDs. Treat it as sensitive when exporting or sharing.
- All API traffic is HTTPS; LDAPS is optional but recommended.
- TAM does not call out to any telemetry / update / analytics endpoint.
