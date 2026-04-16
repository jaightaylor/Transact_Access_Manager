# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for Transact Access Manager.

Build with:
    pyinstaller transact_access_manager.spec

Or use build.sh / build.bat for a guided build.
"""

import sys
import os

block_cipher = None
script_dir = os.path.dirname(os.path.abspath(SPEC))

# Platform-specific keyring backend
if sys.platform == "darwin":
    keyring_hidden = ["keyring.backends.macOS"]
elif sys.platform == "win32":
    keyring_hidden = ["keyring.backends.Windows"]
else:
    keyring_hidden = ["keyring.backends.SecretService"]

a = Analysis(
    [os.path.join(script_dir, "transact_access_manager.py")],
    pathex=[script_dir],
    binaries=[],
    datas=[],
    hiddenimports=[
        "transact_api",
        "transact_credential_manager",
        "ad_lookup",
        # keyring discovers backends at runtime — PyInstaller misses them
        *keyring_hidden,
        "keyring.backends",
        # ldap3 internals that may be missed
        "ldap3",
        "ldap3.core",
        "ldap3.operation",
        "ldap3.protocol",
        "ldap3.strategy",
        "ldap3.utils",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Trim things we don't need
        "matplotlib",
        "numpy",
        "scipy",
        "pandas",
        "PIL",
        "pytest",
        "setuptools",
        "wheel",
        "pip",
    ],
    noarchive=False,
    optimize=0,
    cipher=block_cipher,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="Transact Access Manager",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,           # Windowed mode — no terminal
    disable_windowed_traceback=False,
    argv_emulation=True,     # macOS: allows drag-and-drop, etc.
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

# macOS .app bundle
if sys.platform == "darwin":
    app = BUNDLE(
        exe,
        name="Transact Access Manager.app",
        icon=None,           # Set to "icon.icns" if you add one
        bundle_identifier="edu.uccs.transact-access-manager",
        info_plist={
            "CFBundleShortVersionString": "1.0.0",
            "NSHighResolutionCapable": True,
        },
    )
