#!/usr/bin/env python3
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
load_dotenv(dotenv_path=Path(__file__).resolve().parent.parent / ".env")


REQUIRED_ENV = [
    "AIRTABLE_PAT",
    "AIRTABLE_BASE_ID",
]

OPTIONAL_ENV = [
    ("AIRTABLE_SESSIONS_TABLE", "Sessions"),
    ("AIRTABLE_CONTACTS_TABLE", "Contacts"),
    ("SELF_EMAILS", ""),
]

REQUIRED_FILES = [
    "sync_last_4_weeks.py",
    "credentials.json",
]

OPTIONAL_FILES = [
    ".env",
    "token.json",
    "requirements.txt",
]

def fail(msg: str) -> None:
    print(f"✗ {msg}")
    sys.exit(1)

def ok(msg: str) -> None:
    print(f"✓ {msg}")

def warn(msg: str) -> None:
    print(f"! {msg}")

def main() -> None:
    root = Path(__file__).resolve().parent.parent

    # venv sanity: ensure we're running inside .venv
    exe = Path(sys.executable).resolve()

    in_venv = (
        hasattr(sys, "real_prefix")
        or (hasattr(sys, "base_prefix") and sys.base_prefix != sys.prefix)
        or (os.environ.get("VIRTUAL_ENV") is not None)
    )

    if not in_venv:
        warn(f"Not running inside a virtualenv (python = {exe})")
        warn("Activate it with: source .venv/bin/activate")
    else:
        venv = os.environ.get("VIRTUAL_ENV")
        if venv and Path(venv).name != ".venv":
            warn(f"Running in a venv, but not .venv (VIRTUAL_ENV = {venv})")
        else:
            ok(f"Using venv python: {exe}")


    # required files
    for f in REQUIRED_FILES:
        p = root / f
        if not p.exists():
            fail(f"Missing required file: {f}")
        ok(f"Found {f}")

    for f in OPTIONAL_FILES:
        p = root / f
        if p.exists():
            ok(f"Found {f}")
        else:
            warn(f"Optional file not found: {f}")

    # env vars
    # If .env exists, remind user to load it
    env_path = root / ".env"
    if env_path.exists():
        ok(".env present (good). Your script should load it.")
    else:
        warn(".env missing. You can create one to store AIRTABLE_PAT/AIRTABLE_BASE_ID.")

    missing = []
    for k in REQUIRED_ENV:
        v = (os.getenv(k) or "").strip()
        if not v:
            missing.append(k)
        else:
            if k == "AIRTABLE_PAT":
                ok(f"{k} set (starts with: {(v[:6])}...)")
            else:
                ok(f"{k} set ({v})")

    for k, default in OPTIONAL_ENV:
        v = (os.getenv(k) or "").strip()
        if v:
            ok(f"{k} set ({v})")
        else:
            ok(f"{k} not set (default: {default})")

    if missing:
        print("")
        print("Missing required env vars:")
        for k in missing:
            print(f"  - {k}")
        print("")
        print("Fix options:")
        print("  1) Put them in .env in the project root")
        print("  2) Or export them in your shell")
        sys.exit(1)

    ok("Doctor checks passed.")

if __name__ == "__main__":
    main()
