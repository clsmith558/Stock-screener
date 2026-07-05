"""Centralized contact identity for SEC EDGAR and HTTP user-agent strings."""

from __future__ import annotations

import os

APP_NAME = os.environ.get("SCREENER_APP_NAME", "SignalScreener")
CONTACT_EMAIL = os.environ.get("SCREENER_CONTACT_EMAIL", "").strip()


def sec_edgar_identity() -> str:
    return (os.environ.get("SEC_EDGAR_IDENTITY") or CONTACT_EMAIL).strip()


def sec_user_agent() -> str:
    identity = sec_edgar_identity()
    if not identity:
        return APP_NAME
    return f"{APP_NAME} {identity}"


def wiki_user_agent() -> str:
    email = CONTACT_EMAIL or "you@example.com"
    return f"{APP_NAME}/1.0 (research; contact: {email})"