"""
Gmail PDF fetcher for Casa Capital
----------------------------------

This script connects to Gmail via the Gmail API, finds recent messages
with PDF attachments (statements, applications, etc.), and saves:

- The PDF files into the `samples/` folder
- A small `email_metadata.json` next to each set of attachments

Prerequisites
-------------
1. Install dependencies (in your venv):

    pip install google-api-python-client google-auth-httplib2 google-auth-oauthlib

2. Follow the README instructions to create a Google Cloud project,
   enable the Gmail API, and download `credentials.json` into:

    D:\\Project\\Nick_CasaCapital_LLM\\mca-ocr-worker\\credentials.json

Usage
-----
From the `mca-ocr-worker` folder (venv active):

    python src/gmail_fetcher.py --query \"has:attachment filename:pdf\" --max-results 20

You can filter further, e.g.:

    python src/gmail_fetcher.py --query \"label:bank-statements has:attachment filename:pdf\"

By default, PDFs are written to `samples/` and metadata to
`casa-capital/deals/<slug>/email_metadata.json` if that tree exists.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import List, Dict, Any

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from datetime import datetime


# If modifying these scopes, delete the token.json file.
# Include both so token response matches (Google may return readonly when include_granted_scopes is used).
SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.readonly",
]

ROOT_DIR = Path(__file__).resolve().parent.parent  # mca-ocr-worker
PROJECT_ROOT = ROOT_DIR.parent                    # Nick_CasaCapital_LLM
SAMPLES_DIR = ROOT_DIR / "samples"
TOKEN_PATH = ROOT_DIR / "token.json"
CREDENTIALS_PATH = ROOT_DIR / "credentials.json"


@dataclass
class EmailMetadata:
    message_id: str
    thread_id: str
    subject: str
    sender: str
    recipient: str
    date: str
    snippet: str
    pdf_files: List[str]


# Redirect URI for headless OAuth (no browser). Add this exact URI in Google Cloud Console
# under APIs & Services → Credentials → your OAuth client → Authorized redirect URIs.
HEADLESS_REDIRECT_URI = "http://localhost:8080/"


def _run_oauth_headless(flow: "InstalledAppFlow") -> Credentials:
    """Run OAuth flow by printing the URL and reading the redirect URL (for headless/VM)."""
    # Allow http://localhost redirect (oauthlib otherwise requires HTTPS)
    os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"
    flow.redirect_uri = HEADLESS_REDIRECT_URI
    auth_url, _ = flow.authorization_url(
        access_type="offline",
        prompt="consent",
        include_granted_scopes="true",
    )
    print("\nNo browser available (e.g. on a server). Complete auth on your computer:\n")
    print("1. Open this URL in your browser:")
    print("   ", auth_url)
    print()
    print("2. After signing in and allowing access, you will be redirected to a page that may not load.")
    print("3. Copy the ENTIRE URL from your browser's address bar and paste it below:")
    print()
    redirect_url = input("Paste the redirect URL here: ").strip()
    if not redirect_url:
        raise SystemExit("No URL pasted. Exiting.")
    flow.fetch_token(authorization_response=redirect_url)
    return flow.credentials


def get_gmail_service() -> Any:
    """Authenticate and return a Gmail API service instance."""
    creds: Credentials | None = None

    if TOKEN_PATH.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)

    # If there are no (valid) credentials, let the user log in.
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not CREDENTIALS_PATH.exists():
                raise FileNotFoundError(
                    f"credentials.json not found at {CREDENTIALS_PATH}. "
                    "Follow README steps to download it from Google Cloud."
                )
            flow = InstalledAppFlow.from_client_secrets_file(
                str(CREDENTIALS_PATH), SCOPES
            )
            # On headless servers (e.g. GCP VM) there is no browser; use manual paste flow.
            try:
                creds = flow.run_local_server(port=0)
            except Exception as e:
                if "could not locate runnable browser" in str(e) or "webbrowser" in str(e).lower():
                    flow = InstalledAppFlow.from_client_secrets_file(
                        str(CREDENTIALS_PATH), SCOPES
                    )
                    creds = _run_oauth_headless(flow)
                else:
                    raise
        # Save the credentials for the next run
        with TOKEN_PATH.open("w") as token:
            token.write(creds.to_json())

    service = build("gmail", "v1", credentials=creds)
    return service


def list_messages(service: Any, user_id: str, query: str, max_results: int) -> List[Dict]:
    """List message IDs matching the Gmail search query."""
    response = (
        service.users()
        .messages()
        .list(userId=user_id, q=query, maxResults=max_results)
        .execute()
    )
    return response.get("messages", [])


def safe_filename(name: str, email_date: str | None = None) -> str:
    """
    Make a filesystem-safe, mostly-unique filename.

    We append a timestamp (with microseconds) to avoid collisions when
    different emails have attachments with the same original name.
    """
    base = "".join(c for c in name if c not in r"<>:\"/\\|?*").strip()

    # Try to derive timestamp from email Date header; fall back to now.
    ts = None
    if email_date:
        try:
            # Example: 'Mon, 24 Feb 2026 13:45:12 -0500'
            parsed = datetime.strptime(email_date[:31], "%a, %d %b %Y %H:%M:%S %z")
            ts = parsed
        except Exception:
            pass
    if ts is None:
        ts = datetime.now()

    # Many email Date headers only have second precision, so microseconds are 0.
    # Ensure we always have a non-zero microsecond component to avoid collisions.
    if ts.microsecond == 0:
        ts = ts.replace(microsecond=datetime.now().microsecond)

    stamp = ts.strftime("%Y%m%d_%H%M%S_%f")

    # Insert timestamp before extension
    if "." in base:
        stem, ext = base.rsplit(".", 1)
        base = f"{stem}_{stamp}.{ext}"
    else:
        base = f"{base}_{stamp}"

    return base


def slug_from_subject(subject: str) -> str:
    """Create a simple slug from email subject."""
    base = subject.strip() or "deal"
    base = base.replace(" ", "_")
    base = "".join(ch for ch in base if ch.isalnum() or ch in ("_", "-"))
    return base.lower()[:60]


def _timestamp_from_email_date(email_date: str | None) -> str:
    """Parse email Date header into a slug-safe timestamp (YYYYMMDD_HHMMSS)."""
    if not email_date:
        return datetime.now().strftime("%Y%m%d_%H%M%S")
    try:
        parsed = datetime.strptime(email_date[:31], "%a, %d %b %Y %H:%M:%S %z")
        return parsed.strftime("%Y%m%d_%H%M%S")
    except Exception:
        return datetime.now().strftime("%Y%m%d_%H%M%S")


def deal_slug(subject: str, message_id: str, email_date: str | None = None) -> str:
    """
    Unique per-email slug.

    Uses subject slug + timestamp from email date so that multiple
    emails with the same subject get separate deal folders (e.g.
    abundia_lc_corp_20260225_143022). Falls back to message ID if
    no email_date is provided.
    """
    base = slug_from_subject(subject) or "deal"
    if email_date is not None:
        suffix = _timestamp_from_email_date(email_date)
    else:
        suffix = (message_id or "").replace(" ", "")[:8] or "msg"
    return f"{base}_{suffix}"


def save_attachments_and_metadata(
    service: Any, user_id: str, msg_id: str
) -> List[EmailMetadata]:
    """Download PDF attachments for a single message and write metadata."""
    msg = service.users().messages().get(userId=user_id, id=msg_id).execute()

    headers = {h["name"].lower(): h["value"] for h in msg.get("payload", {}).get("headers", [])}

    subject = headers.get("subject", "")
    sender = headers.get("from", "")
    recipient = headers.get("to", "")
    date = headers.get("date", "")
    snippet = msg.get("snippet", "")

    parts = msg.get("payload", {}).get("parts", []) or []

    # Determine base directory for attachments and metadata
    deals_root = PROJECT_ROOT / "casa-capital" / "deals"
    deal_dir = None
    if deals_root.exists():
        slug = deal_slug(subject, msg.get("id", ""), email_date=date)
        deal_dir = deals_root / slug
        deal_dir.mkdir(parents=True, exist_ok=True)
        attachment_base = deal_dir
    else:
        SAMPLES_DIR.mkdir(parents=True, exist_ok=True)
        attachment_base = SAMPLES_DIR

    pdf_files: List[str] = []
    for part in parts:
        filename = part.get("filename")
        body = part.get("body", {})
        if not filename or not filename.lower().endswith(".pdf"):
            continue

        att_id = body.get("attachmentId")
        if not att_id:
            continue

        att = (
            service.users()
            .messages()
            .attachments()
            .get(userId=user_id, messageId=msg_id, id=att_id)
            .execute()
        )
        data = att.get("data")
        if not data:
            continue

        file_bytes = base64.urlsafe_b64decode(data.encode("UTF-8"))

        safe_name = safe_filename(filename, email_date=date)
        attachment_base.mkdir(parents=True, exist_ok=True)
        out_path = attachment_base / safe_name
        with out_path.open("wb") as f:
            f.write(file_bytes)

        # Store paths relative to PROJECT_ROOT so we can span sibling dirs
        pdf_files.append(str(out_path.relative_to(PROJECT_ROOT)))
        print(f"Saved PDF: {out_path}")

    if not pdf_files:
        return []

    meta = EmailMetadata(
        message_id=msg.get("id", ""),
        thread_id=msg.get("threadId", ""),
        subject=subject,
        sender=sender,
        recipient=recipient,
        date=date,
        snippet=snippet,
        pdf_files=pdf_files,
    )

    # Write metadata JSON into casa-capital/deals if that tree exists
    if deal_dir is not None:
        meta_path = deal_dir / "email_metadata.json"
        with meta_path.open("w", encoding="utf-8") as f:
            json.dump(asdict(meta), f, indent=2)
        print(f"Saved email metadata: {meta_path}")

    return [meta]


def mark_as_read(service: Any, user_id: str, msg_id: str) -> None:
    """Remove UNREAD label from a message."""
    service.users().messages().modify(
        userId=user_id,
        id=msg_id,
        body={"removeLabelIds": ["UNREAD"]},
    ).execute()


def main():
    parser = argparse.ArgumentParser(description="Fetch PDF attachments from Gmail.")
    parser.add_argument(
        "--query",
        type=str,
        default="has:attachment filename:pdf",
        help="Gmail search query (e.g. 'has:attachment filename:pdf from:bank.com').",
    )
    parser.add_argument(
        "--max-results",
        type=int,
        default=20,
        help="Maximum number of messages to fetch.",
    )
    args = parser.parse_args()

    service = get_gmail_service()
    user_id = "me"

    print(f"Searching Gmail with query: {args.query!r}")
    messages = list_messages(service, user_id, args.query, args.max_results)

    if not messages:
        print("No matching messages found.")
        return

    all_meta: List[EmailMetadata] = []
    for msg in messages:
        all_meta.extend(save_attachments_and_metadata(service, user_id, msg["id"]))

    print(f"\nDone. Downloaded PDFs from {len(all_meta)} message(s).")


if __name__ == "__main__":
    main()

