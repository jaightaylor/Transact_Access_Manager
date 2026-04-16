# Transact Access Manager (TAM)

Desktop GUI application for managing student and employee door access and board/meal plan assignments at UCCS through the Transact BBTS Management API. Replaces manual CSV import workflows with direct, batched API operations.

## Features

- **Bulk user import** from CSV or text files with automatic delimiter detection
- **Active Directory resolution** — resolve student IDs, emails, or usernames to Transact customer records via AD/LDAP
- **Door access management** — stage and commit add/remove operations for door access plans
- **Board & meal plan management** — stage and commit add/remove operations with priority, date range, and active status
- **Card operations** — update card status, mark as lost, retire cards, retire mobile credentials
- **Batch compare** — compare current assignments against a loaded CSV to identify differences
- **Profile saving** — save and reload plan selection presets between sessions
- **Current state export** — export a CSV report of all current assignments for a user set
- **Audit logging** — append-only log of all API operations with timestamps
- **Cross-platform** — runs on macOS, Windows, and Linux (pyad on Windows, ldap3 elsewhere)
- **Secure credential storage** — OAuth keys and AD credentials stored in OS keychain via keyring

## Quick Start

### macOS / Linux

```bash
./run.sh
```

### Windows

Double-click `run.bat` or run it from a terminal.

Both scripts create a virtual environment, install dependencies, and launch the application.

### Manual Setup

```bash
python3 -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements_transact.txt
python transact_access_manager.py
```

## First Run

On first launch, the app will prompt for two sets of credentials:

### Transact OAuth Credentials

| Field | Description |
|-------|-------------|
| Consumer Key | OAuth 1.0 consumer key for BBTS API |
| Consumer Secret | OAuth 1.0 consumer secret |
| Hostname | Transact server (e.g., `bbts.uccs.edu`) |
| Route Scheme | Routing scheme (e.g., `Institution`) |
| Route Value | Routing value (e.g., `UCCSInstitution`) |

### Active Directory / LDAP Credentials

| Field | Description |
|-------|-------------|
| Server | AD/LDAP server address |
| Username | Bind username (e.g., `DOMAIN\user`) |
| Password | Bind password |
| Use SSL | Enable LDAPS (recommended) |

All credentials are stored in the OS keychain — never written to disk as plaintext.

## Workflow

1. **Load** a CSV or text file containing user identifiers (student IDs, emails, etc.)
2. **Resolve** identifiers against Active Directory to find Transact customer numbers
3. **Connect** to the Transact API to load available door access and board/meal plans
4. **Stage** add or remove operations for selected users and plans
5. **Review** staged actions before committing
6. **Commit** — the app executes all staged operations via the Transact API
7. **Export** current state reports as needed

## Project Structure

```
transact_access_manager.py       # Main GUI application (tkinter)
transact_api.py                  # OAuth 1.0 HMAC-SHA1 API client
transact_credential_manager.py   # Credential storage (keyring + JSON settings)
ad_lookup.py                     # Cross-platform AD/LDAP query client
requirements_transact.txt        # Python dependencies
transact_access_manager.spec     # PyInstaller spec for building executables
```

## Requirements

- Python 3.10+
- Dependencies: `requests`, `oauthlib`, `keyring`, `ldap3`
- Optional: `pyad` (Windows-native AD support)

## Building an Executable

```bash
pip install pyinstaller
pyinstaller transact_access_manager.spec
```

Output: `dist/Transact Access Manager.app` (macOS) or `dist/Transact Access Manager.exe` (Windows)

## Configuration

Runtime configuration is stored in `.transact_access_configs/` (created automatically):

- `settings.json` — non-secret preferences (delimiter, selected fields, etc.)
- `profiles/` — saved plan selection presets
- `audit.log` — append-only audit trail of API operations
