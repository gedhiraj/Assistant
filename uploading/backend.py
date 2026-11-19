import os
import re
import json
import time
import html
import uuid
import base64
import asyncio
import datetime
import logging
import certifi
import threading
from functools import wraps
from typing import Literal, Optional
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid, parseaddr
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request as FastAPIRequest
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, FileResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from pymongo import MongoClient, DESCENDING, ASCENDING

from google import genai
from google.genai import types
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from calendar_tools import (
    CalendarEventInput,
    calendar_api_error,
    calendar_attendees,
    calendar_event_body,
    calendar_service,
    parse_calendar_date,
    parse_calendar_datetime,
    public_calendar_event,
)
from sheets_tools import (
    csv_export,
    grid_range,
    office_export,
    parse_a1_range,
    professional_format_requests,
    quoted_sheet_range,
    sanitize_download_filename,
)

load_dotenv()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
CREDENTIALS_FILE = os.getenv("GOOGLE_CREDENTIALS_FILE", "credentials.json")
LEGACY_TOKEN_FILE = os.getenv("GOOGLE_TOKEN_FILE", "token.json")
MONGODB_URI = os.getenv("MONGODB_URI")
MONGODB_DB_NAME = os.getenv("MONGODB_DB_NAME", "d25_assistant")

if not GEMINI_API_KEY:
    raise ValueError("Missing GEMINI_API_KEY in .env file.")
if not MONGODB_URI:
    raise ValueError("Missing MONGODB_URI in .env file.")

SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/tasks",
    "https://www.googleapis.com/auth/contacts.readonly",
]

FALLBACK_MODELS = [
    "gemini-3.5-flash-lite",
    "gemini-3.8-flash",
    "gemini-3.1-flash-lite",
    "gemini-flash-latest",
]

app = FastAPI(title="d25 Assistant Backend")
LOGGER = logging.getLogger(__name__)
BASE_DIR = Path(__file__).resolve().parent
TOKENS_DIR = BASE_DIR / "tokens"
ACTIVE_USER_FILE = TOKENS_DIR / "active_user.json"
REDIRECT_URI = "http://127.0.0.1:8000/api/auth/callback"

TOKENS_DIR.mkdir(parents=True, exist_ok=True)
(BASE_DIR / "static").mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# MongoDB Atlas Initialization
# ---------------------------------------------------------------------------
MONGO_CLIENT = MongoClient(
    MONGODB_URI,
    tls=True,
    tlsCAFile=certifi.where(),
    serverSelectionTimeoutMS=15000,
)
DB = MONGO_CLIENT[MONGODB_DB_NAME]
SESSIONS_COL = DB["sessions"]
MESSAGES_COL = DB["messages"]
PERMISSIONS_COL = DB["permissions"]

SESSIONS_COL.create_index([("user_email", ASCENDING), ("updated_at", DESCENDING)])
SESSIONS_COL.create_index("session_id", unique=True)
MESSAGES_COL.create_index([("session_id", ASCENDING), ("created_at", ASCENDING)])
PERMISSIONS_COL.create_index("user_email", unique=True)

AI_CLIENT = genai.Client(api_key=GEMINI_API_KEY)
OAUTH_STATES: dict[str, Flow] = {}


# ---------------------------------------------------------------------------
# Multi-Account Google OAuth & Active Session Management
# ---------------------------------------------------------------------------
def _token_path_for(email: str) -> Path:
    safe_name = re.sub(r"[^a-zA-Z0-9_.-]", "_", email.lower().strip())
    return TOKENS_DIR / f"{safe_name}.json"


def get_active_user_email() -> Optional[str]:
    if ACTIVE_USER_FILE.exists():
        try:
            data = json.loads(ACTIVE_USER_FILE.read_text(encoding="utf-8"))
            return data.get("email")
        except Exception:
            return None
    return None


def set_active_user_email(email: Optional[str]) -> None:
    if email is None:
        if ACTIVE_USER_FILE.exists():
            ACTIVE_USER_FILE.unlink()
    else:
        ACTIVE_USER_FILE.write_text(json.dumps({"email": email.lower().strip()}), encoding="utf-8")


def get_credentials_for_active_user() -> tuple[Optional[str], Optional[Credentials]]:
    email = get_active_user_email()
    if not email:
        return None, None

    tpath = _token_path_for(email)
    if not tpath.exists():
        set_active_user_email(None)
        return None, None

    creds = Credentials.from_authorized_user_file(str(tpath), SCOPES)
    if not creds.valid:
        if creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
                tpath.write_text(creds.to_json(), encoding="utf-8")
            except Exception:
                set_active_user_email(None)
                return None, None
        else:
            set_active_user_email(None)
            return None, None

    return email, creds


def require_gmail_service():
    email, creds = get_credentials_for_active_user()
    if not email or not creds:
        raise HTTPException(status_code=401, detail="Not signed in with Google.")
    return email, build("gmail", "v1", credentials=creds)


# ---------------------------------------------------------------------------
# MongoDB-Backed "Allow Always" Permissions (Per User)
# ---------------------------------------------------------------------------
def _extract_raw_email(header_val: str) -> str:
    val = header_val.strip().lower()
    if "<" in val and ">" in val:
        return val.split("<")[-1].split(">")[0].strip()
    return val


def load_permissions(user_email: str) -> dict:
    doc = PERMISSIONS_COL.find_one({"user_email": user_email}, {"_id": 0})
    if not doc:
        return {"user_email": user_email, "allowed_recipients": []}
    return doc


def is_recipient_trusted(recipient_email: str, user_email: str) -> bool:
    clean_addr = _extract_raw_email(recipient_email)
    doc = load_permissions(user_email)
    return clean_addr in [e.lower() for e in doc.get("allowed_recipients", [])]


def trust_recipient_always(recipient_email: str, user_email: str) -> None:
    clean_addr = _extract_raw_email(recipient_email)
    if not clean_addr:
        return
    PERMISSIONS_COL.update_one(
        {"user_email": user_email},
        {
            "$addToSet": {"allowed_recipients": clean_addr},
            "$set": {"updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat()},
        },
        upsert=True,
    )


# ---------------------------------------------------------------------------
# Pending Approval State & API Models
# ---------------------------------------------------------------------------
PENDING_APPROVALS: dict[str, dict] = {}
ACTIVE_CHAT_TURNS: dict[str, dict] = {}
ACTIVE_CHAT_TURNS_LOCK = threading.Lock()
SHEETS_DOWNLOADS: dict[str, dict] = {}
SHEETS_DOWNLOAD_LOCK = threading.Lock()
SHEETS_DOWNLOAD_TTL_SECONDS = 1800
MAX_SHEETS_DOWNLOAD_BYTES = 25 * 1024 * 1024
MAX_SHEETS_DOWNLOADS = 20
MAX_SHEETS_DOWNLOAD_STORAGE_BYTES = 80 * 1024 * 1024


class ChatRequest(BaseModel):
    session_id: Optional[str] = None
    turn_id: Optional[str] = None
    message: str
    mode: Literal["ask", "plan", "act"] = "ask"


class ChatStopRequest(BaseModel):
    turn_id: str


class ApprovalEdits(BaseModel):
    to: Optional[str] = None
    subject: Optional[str] = None
    body: Optional[str] = None
    signature: Optional[str] = None


class ApprovalDecision(BaseModel):
    approval_id: str
    decision: str  # "allow_once" | "allow_always" | "deny"
    edits: Optional[ApprovalEdits] = None


# ---------------------------------------------------------------------------
# Human-Style Editorial HTML Template
# ---------------------------------------------------------------------------
def _clean_markdown_to_html(text: str) -> str:
    escaped = html.escape(text)
    escaped = re.sub(r"\*\*(.+?)\*\*", r'<strong style="color: #111827; font-weight: 600;">\1</strong>', escaped)
    return escaped


def build_html_email(clean_body: str, sender_name: str = "Dhiraj Kumar") -> str:
    blocks = [b.strip() for b in clean_body.split("\n\n") if b.strip()]
    html_blocks = []

    for block in blocks:
        lines = [line.strip() for line in block.split("\n") if line.strip()]
        if all(line.startswith(("- ", "* ")) for line in lines):
            items_html = "".join(
                f'<li style="margin: 0 0 8px 0; line-height: 1.6; color: #1f2328; font-size: 15px;">'
                f'{_clean_markdown_to_html(line[2:].strip())}</li>'
                for line in lines
            )
            html_blocks.append(f'<ul style="margin: 0 0 16px 0; padding-left: 20px;">{items_html}</ul>')
        else:
            formatted = "<br>".join(_clean_markdown_to_html(line) for line in lines)
            html_blocks.append(
                f'<p style="margin: 0 0 16px 0; line-height: 1.65; color: #1f2328; font-size: 15px;">{formatted}</p>'
            )

    body_html = "".join(html_blocks)
    signature_html = (
        f'<p style="margin: 28px 0 0 0; font-size: 15px; font-weight: 600; color: #111827;">{html.escape(sender_name)}</p>'
        if sender_name
        else ""
    )
    return f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
</head>
<body style="margin: 0; padding: 32px 24px; background-color: #ffffff; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; -webkit-font-smoothing: antialiased;">
  <table width="100%" cellpadding="0" cellspacing="0" border="0">
    <tr>
      <td align="left">
        <table width="100%" cellpadding="0" cellspacing="0" border="0" style="max-width: 580px;">
          <tr>
            <td>
              {body_html}
              {signature_html}
            </td>
          </tr>
        </table>
      </td>
    </tr>
  </table>
</body>
</html>"""


def _set_sender_headers(message: EmailMessage, sender_name: str, sender_email: str) -> None:
    message["From"] = formataddr((sender_name, sender_email))
    message["Date"] = formatdate(localtime=True)
    message["Message-ID"] = make_msgid(domain=sender_email.rsplit("@", 1)[1])


def _extract_email_body(payload: dict) -> str:
    if "parts" in payload:
        for part in payload["parts"]:
            if part.get("mimeType") == "text/plain" and "data" in part.get("body", {}):
                return base64.urlsafe_b64decode(part["body"]["data"]).decode("utf-8", errors="replace")
            if "parts" in part:
                nested = _extract_email_body(part)
                if nested:
                    return nested
    elif "data" in payload.get("body", {}):
        return base64.urlsafe_b64decode(payload["body"]["data"]).decode("utf-8", errors="replace")
    return ""


# ---------------------------------------------------------------------------
# SSE Event Helper & MongoDB-Logged Approval Gate
# ---------------------------------------------------------------------------
def format_sse(event_type: str, payload: dict) -> str:
    return f"data: {json.dumps({'type': event_type, **payload})}\n\n"


def request_user_approval_sync(
    event_queue: asyncio.Queue,
    loop: asyncio.AbstractEventLoop,
    user_email: str,
    turn_record: dict,
    action_type: str,
    recipient: str,
    preview_data: dict,
    cancellation_event: Optional[threading.Event] = None,
) -> str:
    """Pauses execution for approval unless recipient is trusted, and logs the approval record to MongoDB."""
    if cancellation_event and cancellation_event.is_set():
        return "deny"
    clean_recip = _extract_raw_email(recipient) if recipient else ""

    if clean_recip and is_recipient_trusted(clean_recip, user_email):
        step_obj = {
            "id": str(uuid.uuid4()),
            "status": "completed",
            "label": f"Auto-approved for trusted recipient ({clean_recip})",
            "detail": "",
        }
        turn_record["steps"].append(step_obj)
        turn_record["approvals"].append({
            "approval_id": f"auto-{uuid.uuid4().hex[:8]}",
            "action_type": action_type,
            "recipient": clean_recip,
            "preview": preview_data,
            "decision": "allow_always (auto)",
        })
        loop.call_soon_threadsafe(event_queue.put_nowait, format_sse("step", step_obj))
        return "allow_always"

    approval_id = str(uuid.uuid4())
    decision_event = asyncio.Event()
    PENDING_APPROVALS[approval_id] = {
        "event": decision_event,
        "decision": "deny",
        "recipient": clean_recip,
        "action_type": action_type,
        "preview": preview_data,
    }

    loop.call_soon_threadsafe(
        event_queue.put_nowait,
        format_sse(
            "approval_required",
            {
                "approval_id": approval_id,
                "action_type": action_type,
                "recipient": clean_recip,
                "preview": preview_data,
            },
        ),
    )

    future = asyncio.run_coroutine_threadsafe(decision_event.wait(), loop)
    while True:
        if cancellation_event and cancellation_event.is_set():
            future.cancel()
            PENDING_APPROVALS.pop(approval_id, None)
            turn_record["approvals"].append({
                "approval_id": approval_id,
                "action_type": action_type,
                "recipient": clean_recip,
                "preview": preview_data,
                "decision": "cancelled",
            })
            return "deny"
        try:
            future.result(timeout=0.1)
            break
        except TimeoutError:
            continue

    record = PENDING_APPROVALS.pop(approval_id, {})
    decision = record.get("decision", "deny")
    clean_recip = record.get("recipient", clean_recip)

    if decision == "allow_always" and clean_recip:
        trust_recipient_always(clean_recip, user_email)

    turn_record["approvals"].append({
        "approval_id": approval_id,
        "action_type": action_type,
        "recipient": clean_recip,
        "preview": preview_data,
        "decision": decision,
    })

    return decision


# ---------------------------------------------------------------------------
# Gemini History Serialization for MongoDB Multi-Turn Continuity
# ---------------------------------------------------------------------------
def load_gemini_history_from_db(session_id: str, user_email: str) -> list[types.Content]:
    doc = SESSIONS_COL.find_one({"session_id": session_id, "user_email": user_email})
    if not doc or not doc.get("gemini_history"):
        return []
    restored = []
    for item in doc["gemini_history"]:
        restored.append(
            types.Content(
                role=item["role"],
                parts=[types.Part.from_text(text=item["text"])],
            )
        )
    return restored


def save_turn_to_mongodb(
    session_id: str,
    user_email: str,
    user_message: str,
    assistant_reply: str,
    turn_record: dict,
    duration_sec: int,
):
    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
    title = user_message[:48] + ("..." if len(user_message) > 48 else "")

    existing = SESSIONS_COL.find_one({"session_id": session_id, "user_email": user_email})
    if not existing:
        SESSIONS_COL.insert_one({
            "session_id": session_id,
            "user_email": user_email,
            "title": title,
            "created_at": now_iso,
            "updated_at": now_iso,
            "gemini_history": [
                {"role": "user", "text": user_message},
                {"role": "model", "text": assistant_reply},
            ],
        })
    else:
        SESSIONS_COL.update_one(
            {"session_id": session_id, "user_email": user_email},
            {
                "$set": {"updated_at": now_iso},
                "$push": {
                    "gemini_history": {
                        "$each": [
                            {"role": "user", "text": user_message},
                            {"role": "model", "text": assistant_reply},
                        ]
                    }
                },
            },
        )

    MESSAGES_COL.insert_one({
        "message_id": str(uuid.uuid4()),
        "session_id": session_id,
        "user_email": user_email,
        "user_message": user_message,
        "assistant_reply": assistant_reply,
        "mode": turn_record.get("mode", "ask"),
        "plan_ready": turn_record.get("plan_ready", False),
        "steps": turn_record["steps"],
        "approvals": turn_record["approvals"],
        "duration_sec": duration_sec,
        "created_at": now_iso,
    })


_STANDALONE_GREETING = re.compile(
    r"(?:hi|hello|hey|hiya|howdy|good\s+(?:morning|afternoon|evening))"
    r"(?:[,\s]+(?:there|d25|assistant))?[!?.\s]*",
    re.IGNORECASE,
)


def _quick_greeting_reply(user_message: str) -> Optional[str]:
    if _STANDALONE_GREETING.fullmatch(user_message.strip()):
        return "Hello! How can I help with your emails, calendar, or spreadsheets today?"
    return None


# ---------------------------------------------------------------------------
# Agent Loop with Live Copilot-Style Step Emission + MongoDB Logging
# ---------------------------------------------------------------------------
def run_agent_turn(
    session_id: str,
    user_email: str,
    gmail_service,
    user_message: str,
    mode: str,
    event_queue: asyncio.Queue,
    loop: asyncio.AbstractEventLoop,
    cancellation_event: threading.Event,
):
    start_ts = time.time()
    turn_record = {"steps": [], "approvals": [], "mode": mode}
    display_name = user_email.split("@")[0].replace(".", " ").title()
    step_start_labels: dict[str, str] = {}

    def emit_step(step_id: str, status: str, label: str, detail: str = ""):
        step_start_labels.setdefault(step_id, label)
        step_obj = {"id": step_id, "status": status, "label": label, "detail": detail}
        if status == "completed" and label != step_start_labels[step_id]:
            outcome = label
            step_obj["label"] = step_start_labels[step_id]
            step_obj["detail"] = f"{outcome} - {detail}" if detail else outcome

        for idx, existing in enumerate(turn_record["steps"]):
            if existing["id"] == step_id:
                turn_record["steps"][idx] = step_obj
                break
        else:
            turn_record["steps"].append(step_obj)

        loop.call_soon_threadsafe(event_queue.put_nowait, format_sse("step", step_obj))

    class TurnCancelled(Exception):
        pass

    def ensure_turn_active():
        if cancellation_event.is_set():
            raise TurnCancelled()

    quick_reply = _quick_greeting_reply(user_message)
    if quick_reply is not None:
        if cancellation_event.is_set():
            loop.call_soon_threadsafe(event_queue.put_nowait, format_sse("done", {}))
            return
        greeting_step_id = str(uuid.uuid4())
        emit_step(greeting_step_id, "running", "Thinking...")
        emit_step(greeting_step_id, "completed", "Done")
        duration_sec = max(1, int(time.time() - start_ts))
        if cancellation_event.is_set():
            loop.call_soon_threadsafe(event_queue.put_nowait, format_sse("done", {}))
            return
        save_future = loop.run_in_executor(
            None,
            save_turn_to_mongodb,
            session_id,
            user_email,
            user_message,
            quick_reply,
            turn_record,
            duration_sec,
        )

        def log_save_failure(future):
            error = future.exception()
            if error is not None:
                LOGGER.error(
                    "Could not save quick greeting to conversation history.",
                    exc_info=(type(error), error, error.__traceback__),
                )

        save_future.add_done_callback(log_save_failure)
        loop.call_soon_threadsafe(
            event_queue.put_nowait,
            format_sse("message", {"text": quick_reply}),
        )
        loop.call_soon_threadsafe(event_queue.put_nowait, format_sse("done", {}))
        return

    # --- Tool 1: Read / Search Primary Inbox ---
    def get_emails(query: str = "", max_results: int = 5, include_all_categories: bool = False) -> list[dict]:
        """Fetches and reads emails from Gmail, defaulting strictly to the Primary Inbox.

        Args:
            query: Gmail search filter (e.g., 'after:2026/11/17', 'is:unread', 'from:gedhiraj@gmail.com', or '' for latest Primary Inbox emails).
            max_results: Number of emails to retrieve (default 5, max 15).
            include_all_categories: Set to True ONLY when the user explicitly asks for a specific sender or all categories.
        """
        step_id = str(uuid.uuid4())
        max_results = min(max(1, max_results), 15)
        clean_query = query.strip()

        if not include_all_categories and "from:" not in clean_query.lower() and "category:" not in clean_query.lower():
            clean_query = f"category:primary {clean_query}".strip()

        emit_step(step_id, "running", "Fetching emails from Primary Inbox", f"Query: {clean_query or 'Primary Inbox'}")

        response = gmail_service.users().messages().list(userId="me", q=clean_query, maxResults=max_results).execute()
        messages = response.get("messages", [])

        if not messages:
            emit_step(step_id, "completed", "Checked Primary Inbox (0 emails matched)", clean_query)
            return [{"info": f"No Primary Inbox emails found matching query: '{clean_query}'"}]

        results = []
        for m in messages:
            detail = gmail_service.users().messages().get(userId="me", id=m["id"], format="full").execute()
            headers = detail.get("payload", {}).get("headers", [])
            subject = next((h["value"] for h in headers if h["name"] == "Subject"), "(No Subject)")
            sender = next((h["value"] for h in headers if h["name"] == "From"), "Unknown")
            date = next((h["value"] for h in headers if h["name"] == "Date"), "")
            message_id_header = next((h["value"] for h in headers if h["name"] == "Message-ID"), "")
            body_text = _extract_email_body(detail.get("payload", {})) or detail.get("snippet", "")

            results.append({
                "id": m["id"],
                "threadId": detail.get("threadId"),
                "message_id_header": message_id_header,
                "from": sender,
                "date": date,
                "subject": subject,
                "body": body_text[:2500],
            })

        emit_step(step_id, "completed", f"Fetched {len(results)} email(s)", "Summarizing contents...")
        return results

    # --- Tool 2: Send New Email ---
    def send_email(to_email: str, subject: str, body: str) -> str:
        """Drafts and sends a new clean HTML + plain-text email via Gmail after permission check.

        Args:
            to_email: The recipient's email address.
            subject: A concise, natural, sentence-case subject line.
            body: Concise, polite, natural body text (do NOT include sign-off; the HTML template adds it automatically).
        """
        draft_step_id = str(uuid.uuid4())
        clean_body = body.replace("\\n", "\n").strip()
        clean_subject = subject.strip()

        emit_step(draft_step_id, "completed", f"Drafted email to {to_email}", f"Subject: {clean_subject}")

        preview_data = {
            "to": to_email,
            "subject": clean_subject,
            "body": clean_body,
            "signature": display_name,
        }
        decision = request_user_approval_sync(
            event_queue=event_queue,
            loop=loop,
            user_email=user_email,
            turn_record=turn_record,
            action_type="send_email",
            recipient=to_email,
            preview_data=preview_data,
            cancellation_event=cancellation_event,
        )

        if decision == "deny" or cancellation_event.is_set():
            emit_step(str(uuid.uuid4()), "warning", f"Cancelled sending email to {to_email}")
            return "User denied permission to send this email."

        to_email = preview_data["to"]
        clean_subject = preview_data["subject"]
        clean_body = preview_data["body"]
        signature = preview_data["signature"]

        send_step_id = str(uuid.uuid4())
        emit_step(send_step_id, "running", f"Sending email to {to_email}...")

        message = EmailMessage()
        _set_sender_headers(message, display_name, user_email)
        message["To"] = to_email
        message["Subject"] = clean_subject
        plain_body = clean_body.replace("**", "")
        if signature:
            plain_body = f"{plain_body}\n\n{signature}" if plain_body else signature
        message.set_content(plain_body)
        message.add_alternative(build_html_email(clean_body, signature), subtype="html")

        encoded_message = base64.urlsafe_b64encode(message.as_bytes()).decode()
        ensure_turn_active()
        gmail_service.users().messages().send(userId="me", body={"raw": encoded_message}).execute()

        emit_step(send_step_id, "completed", f"Sent email to {to_email}", f"Subject: {clean_subject}")
        return f"Email successfully sent to {to_email} with subject '{clean_subject}'."

    # --- Tool 3: Reply Inside Thread ---
    def reply_to_email(message_id: str, body: str) -> str:
        """Replies directly inside an existing Gmail thread after permission check.

        Args:
            message_id: The Gmail message 'id' returned by `get_emails` that you are replying to.
            body: Concise, polite reply text (do NOT include sign-off).
        """
        draft_step_id = str(uuid.uuid4())
        orig = gmail_service.users().messages().get(userId="me", id=message_id, format="metadata").execute()
        headers = orig.get("payload", {}).get("headers", [])
        orig_from = next((h["value"] for h in headers if h["name"] == "From"), "")
        orig_subject = next((h["value"] for h in headers if h["name"] == "Subject"), "")
        orig_msg_id = next((h["value"] for h in headers if h["name"] == "Message-ID"), "")
        thread_id = orig.get("threadId")

        reply_subject = orig_subject if orig_subject.lower().startswith("re:") else f"Re: {orig_subject}"
        clean_body = body.replace("\\n", "\n").strip()

        emit_step(draft_step_id, "completed", f"Drafted reply to {orig_from}", f"Subject: {reply_subject}")

        preview_data = {
            "to": orig_from,
            "subject": reply_subject,
            "body": clean_body,
            "signature": display_name,
        }
        decision = request_user_approval_sync(
            event_queue=event_queue,
            loop=loop,
            user_email=user_email,
            turn_record=turn_record,
            action_type="reply_to_email",
            recipient=orig_from,
            preview_data=preview_data,
            cancellation_event=cancellation_event,
        )

        if decision == "deny" or cancellation_event.is_set():
            emit_step(str(uuid.uuid4()), "warning", f"Cancelled reply to {orig_from}")
            return "User denied permission to send this reply."

        reply_subject = preview_data["subject"]
        clean_body = preview_data["body"]
        signature = preview_data["signature"]

        send_step_id = str(uuid.uuid4())
        emit_step(send_step_id, "running", f"Sending reply to {orig_from}...")

        message = EmailMessage()
        _set_sender_headers(message, display_name, user_email)
        message["To"] = orig_from
        message["Subject"] = reply_subject
        if orig_msg_id:
            message["In-Reply-To"] = orig_msg_id
            message["References"] = orig_msg_id

        plain_body = clean_body.replace("**", "")
        if signature:
            plain_body = f"{plain_body}\n\n{signature}" if plain_body else signature
        message.set_content(plain_body)
        message.add_alternative(build_html_email(clean_body, signature), subtype="html")

        encoded_message = base64.urlsafe_b64encode(message.as_bytes()).decode()
        ensure_turn_active()
        gmail_service.users().messages().send(
            userId="me", body={"raw": encoded_message, "threadId": thread_id}
        ).execute()

        emit_step(send_step_id, "completed", f"Sent reply to {orig_from}")
        return f"Reply sent to {orig_from} on thread '{reply_subject}'."

    # --- Tool 4: Modify / Trash / Mark Read ---
    def modify_emails(message_ids: list[str], action: str) -> str:
        """Marks emails as read, archives them, or moves them to trash after permission check.

        Args:
            message_ids: List of Gmail message IDs to modify.
            action: One of 'mark_read', 'archive', or 'trash'.
        """
        if not message_ids:
            return "No message IDs provided."

        decision = request_user_approval_sync(
            event_queue=event_queue,
            loop=loop,
            user_email=user_email,
            turn_record=turn_record,
            action_type="modify_emails",
            recipient="",
            preview_data={
                "action": action,
                "count": len(message_ids),
                "message_ids": message_ids,
            },
            cancellation_event=cancellation_event,
        )

        if decision == "deny" or cancellation_event.is_set():
            emit_step(str(uuid.uuid4()), "warning", f"Cancelled '{action}' on {len(message_ids)} email(s)")
            return f"User denied permission to {action} emails."

        mod_step_id = str(uuid.uuid4())
        emit_step(mod_step_id, "running", f"Applying '{action}' to {len(message_ids)} email(s)...")

        if action == "mark_read":
            ensure_turn_active()
            gmail_service.users().messages().batchModify(
                userId="me", body={"ids": message_ids, "removeLabelIds": ["UNREAD"]}
            ).execute()
        elif action == "archive":
            ensure_turn_active()
            gmail_service.users().messages().batchModify(
                userId="me", body={"ids": message_ids, "removeLabelIds": ["INBOX"]}
            ).execute()
        elif action == "trash":
            for mid in message_ids:
                ensure_turn_active()
                gmail_service.users().messages().trash(userId="me", id=mid).execute()
        else:
            return f"Unsupported action: {action}"

        emit_step(mod_step_id, "completed", f"Completed '{action}' on {len(message_ids)} email(s)")
        return f"Successfully performed '{action}' on {len(message_ids)} email(s)."

    def calendar_tool_error(step_id: str, label: str, error: Exception) -> str:
        if isinstance(error, HttpError):
            detail = calendar_api_error(error).detail
        elif isinstance(error, HTTPException):
            detail = error.detail
        elif isinstance(error, ValueError):
            detail = str(error) or "The Calendar request contains invalid date or time values."
        else:
            raise error
        emit_step(step_id, "warning", label, str(detail))
        return f"{label}: {detail}"

    def calendar_event_preview(action: str, event: dict, event_id: str = "") -> dict:
        public_event = public_calendar_event(event)
        return {
            "action": action,
            "event_id": event_id or public_event.get("id", ""),
            "summary": public_event.get("summary", "(Untitled event)"),
            "start": public_event.get("start", {}),
            "end": public_event.get("end", {}),
            "location": public_event.get("location", ""),
            "description": public_event.get("description", ""),
            "attendees": [item.get("email", "") for item in public_event.get("attendees", [])],
        }

    def request_calendar_approval(action_type: str, preview: dict) -> str:
        return request_user_approval_sync(
            event_queue=event_queue,
            loop=loop,
            user_email=user_email,
            turn_record=turn_record,
            action_type=action_type,
            recipient="",
            preview_data=preview,
            cancellation_event=cancellation_event,
        )

    def get_calendar_events(
        query: str = "",
        time_min: str = "",
        time_max: str = "",
        max_results: int = 25,
    ) -> list[dict] | str:
        """Searches upcoming Google Calendar events and returns IDs, times, guests, and details.

        Args:
            query: Words to search in event titles, descriptions, or locations.
            time_min: Optional ISO-8601 start of the search window, including a timezone when available.
            time_max: Optional ISO-8601 end of the search window, including a timezone when available.
            max_results: Maximum number of events to return, from 1 to 50.
        """
        step_id = str(uuid.uuid4())
        emit_step(step_id, "running", "Searching Google Calendar", query.strip() or "Upcoming events")
        try:
            now = datetime.datetime.now().astimezone()
            start = parse_calendar_datetime(time_min) if time_min else now
            end = parse_calendar_datetime(time_max) if time_max else start + datetime.timedelta(days=30)
            if end <= start:
                raise HTTPException(status_code=422, detail="Calendar search end must be after its start.")
            results = calendar_service().events().list(
                calendarId="primary",
                timeMin=start.isoformat(),
                timeMax=end.isoformat(),
                maxResults=min(max(int(max_results), 1), 50),
                singleEvents=True,
                orderBy="startTime",
                q=query.strip() or None,
            ).execute()
        except (HttpError, HTTPException, ValueError) as error:
            return calendar_tool_error(step_id, "Could not search Google Calendar", error)

        events = []
        for raw_event in results.get("items", []):
            item = public_calendar_event(raw_event)
            item["description"] = item["description"][:1000]
            item["attendees"] = [attendee["email"] for attendee in item["attendees"]]
            events.append(item)
        emit_step(step_id, "completed", f"Found {len(events)} calendar event(s)", query.strip() or "Upcoming events")
        return events

    def create_calendar_event(
        summary: str,
        start: str = "",
        end: str = "",
        description: str = "",
        location: str = "",
        attendees: Optional[list[str]] = None,
        all_day: bool = False,
        start_date: str = "",
        end_date: str = "",
        reminder_minutes: Optional[int] = 15,
        use_default_reminders: bool = False,
    ) -> str:
        """Prepares a Google Calendar event; it is created only after the user approves the preview.

        Args:
            summary: Event title.
            start: ISO-8601 start time with timezone for a timed event.
            end: ISO-8601 end time with timezone for a timed event.
            description: Optional event details.
            location: Optional location or meeting link.
            attendees: Guest email addresses to invite.
            all_day: True for an all-day event.
            start_date: ISO date for the first day of an all-day event.
            end_date: ISO date for the inclusive last day of an all-day event.
            reminder_minutes: Popup reminder minutes before the event; use null for no reminder.
            use_default_reminders: True to use the user's Calendar defaults.
        """
        step_id = str(uuid.uuid4())
        emit_step(step_id, "running", "Preparing Calendar event for review", summary)
        try:
            payload = CalendarEventInput(
                summary=summary,
                description=description,
                location=location,
                attendees=attendees or [],
                all_day=all_day,
                start=parse_calendar_datetime(start) if start else None,
                end=parse_calendar_datetime(end) if end else None,
                start_date=parse_calendar_date(start_date) if start_date else None,
                end_date=parse_calendar_date(end_date) if end_date else None,
                reminder_minutes=reminder_minutes,
                use_default_reminders=use_default_reminders,
            )
            event_body = calendar_event_body(payload)
        except (HTTPException, ValueError) as error:
            return calendar_tool_error(step_id, "Could not prepare Calendar event", error)

        emit_step(step_id, "completed", "Calendar event ready for review", payload.summary.strip())
        decision = request_calendar_approval(
            "calendar_create",
            calendar_event_preview("Create event", event_body),
        )
        if decision == "deny" or cancellation_event.is_set():
            emit_step(str(uuid.uuid4()), "warning", f"Cancelled Calendar event '{payload.summary.strip()}'")
            return "The user cancelled the Calendar event. It was not created."

        create_step_id = str(uuid.uuid4())
        emit_step(create_step_id, "running", "Creating Google Calendar event", payload.summary.strip())
        try:
            ensure_turn_active()
            created = calendar_service().events().insert(
                calendarId="primary",
                body=event_body,
                sendUpdates="all",
            ).execute()
        except (HttpError, HTTPException) as error:
            return calendar_tool_error(create_step_id, "Could not create Calendar event", error)
        created_event = public_calendar_event(created)
        emit_step(create_step_id, "completed", f"Created Calendar event: {created_event['summary']}")
        return f"Created Calendar event '{created_event['summary']}' and sent guest invitations."

    def update_calendar_event(
        event_id: str,
        summary: Optional[str] = None,
        start: Optional[str] = None,
        end: Optional[str] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        description: Optional[str] = None,
        location: Optional[str] = None,
        attendees: Optional[list[str]] = None,
        all_day: Optional[bool] = None,
        reminder_minutes: Optional[int] = None,
        use_default_reminders: Optional[bool] = None,
    ) -> str:
        """Previews changes to an existing event and applies them only after user approval.

        Args:
            event_id: The ID returned by get_calendar_events; never guess this value.
            summary: New event title, if changing it.
            start: New ISO-8601 timed start, if changing it.
            end: New ISO-8601 timed end, if changing it.
            start_date: New first day for an all-day event.
            end_date: New inclusive last day for an all-day event.
            description: New event details; provide an empty string to clear them.
            location: New location; provide an empty string to clear it.
            attendees: Replacement guest email list.
            all_day: Set only when changing whether this event is all-day.
            reminder_minutes: New popup reminder; use 0 for at-event or null with use_default_reminders=False for none.
            use_default_reminders: Set to choose Calendar defaults or custom reminders.
        """
        lookup_step_id = str(uuid.uuid4())
        emit_step(lookup_step_id, "running", "Loading Calendar event to update", event_id)
        try:
            service = calendar_service()
            current_raw = service.events().get(calendarId="primary", eventId=event_id).execute()
            current = public_calendar_event(current_raw)
            current_start = current.get("start", {})
            current_end = current.get("end", {})
            current_all_day = bool(current_start.get("date"))

            next_reminders = current.get("reminders", {})
            if use_default_reminders is not None or reminder_minutes is not None:
                use_defaults = bool(use_default_reminders)
                next_reminders = (
                    {"useDefault": True}
                    if use_defaults
                    else {
                        "useDefault": False,
                        "overrides": (
                            [{"method": "popup", "minutes": reminder_minutes}]
                            if reminder_minutes is not None
                            else []
                        ),
                    }
                )

            next_all_day = current_all_day if all_day is None else all_day
            schedule_changed = any((start, end, start_date, end_date)) or all_day is not None
            next_start = current_start
            next_end = current_end
            if schedule_changed:
                if next_all_day:
                    if not current_all_day and not (start_date and end_date):
                        raise HTTPException(
                            status_code=422,
                            detail="Provide both start_date and end_date when converting to an all-day event.",
                        )
                    if current_all_day:
                        original_start = parse_calendar_date(current_start["date"])
                        original_end = parse_calendar_date(current_end["date"]) - datetime.timedelta(days=1)
                        new_start = parse_calendar_date(start_date) if start_date else original_start
                        new_end = parse_calendar_date(end_date) if end_date else original_end
                        if start_date and not end_date:
                            new_end = new_start + (original_end - original_start)
                    else:
                        new_start = parse_calendar_date(start_date)
                        new_end = parse_calendar_date(end_date)
                    if new_end < new_start:
                        raise HTTPException(status_code=422, detail="Event end date must be on or after its start date.")
                    next_start = {"date": new_start.isoformat()}
                    next_end = {"date": (new_end + datetime.timedelta(days=1)).isoformat()}
                elif current_all_day:
                    if not (start and end):
                        raise HTTPException(
                            status_code=422,
                            detail="Provide both start and end times when converting an all-day event.",
                        )
                    next_start = {"dateTime": parse_calendar_datetime(start).isoformat()}
                    next_end = {"dateTime": parse_calendar_datetime(end).isoformat()}
                elif start or end:
                    original_start = parse_calendar_datetime(current_start["dateTime"])
                    original_end = parse_calendar_datetime(current_end["dateTime"])
                    next_start_time = parse_calendar_datetime(start) if start else original_start
                    next_end_time = parse_calendar_datetime(end) if end else original_end
                    if start and not end:
                        next_end_time = next_start_time + (original_end - original_start)
                    next_start, next_end = (
                        {"dateTime": next_start_time.isoformat()},
                        {"dateTime": next_end_time.isoformat()},
                    )

            patch = {}
            if summary is not None:
                next_summary = summary.strip()
                if not next_summary or len(next_summary) > 250:
                    raise HTTPException(status_code=422, detail="Event title must be between 1 and 250 characters.")
                patch["summary"] = next_summary
            if description is not None:
                if len(description) > 8000:
                    raise HTTPException(status_code=422, detail="Event description cannot exceed 8000 characters.")
                patch["description"] = description.strip()
            if location is not None:
                if len(location) > 500:
                    raise HTTPException(status_code=422, detail="Event location cannot exceed 500 characters.")
                patch["location"] = location.strip()
            if attendees is not None:
                patch["attendees"] = calendar_attendees(attendees)
            if schedule_changed:
                schedule = calendar_event_body(
                    CalendarEventInput(
                        summary="Calendar event",
                        all_day=next_all_day,
                        start=parse_calendar_datetime(next_start["dateTime"]) if not next_all_day else None,
                        end=parse_calendar_datetime(next_end["dateTime"]) if not next_all_day else None,
                        start_date=parse_calendar_date(next_start["date"]) if next_all_day else None,
                        end_date=(
                            parse_calendar_date(next_end["date"]) - datetime.timedelta(days=1)
                            if next_all_day
                            else None
                        ),
                    )
                )
                next_start, next_end = schedule["start"], schedule["end"]
                patch["start"] = next_start
                patch["end"] = next_end
            if use_default_reminders is not None or reminder_minutes is not None:
                if reminder_minutes is not None and not 0 <= reminder_minutes <= 40320:
                    raise HTTPException(status_code=422, detail="Reminder must be between 0 and 40320 minutes.")
                patch["reminders"] = next_reminders
            if not patch:
                raise HTTPException(status_code=422, detail="Specify at least one Calendar event change.")
            preview_event = dict(current)
            preview_event.update(patch)
            preview_event["reminders"] = next_reminders
        except (HttpError, HTTPException, ValueError) as error:
            return calendar_tool_error(lookup_step_id, "Could not prepare Calendar event update", error)

        emit_step(lookup_step_id, "completed", "Calendar event loaded for review", current["summary"])
        decision = request_calendar_approval(
            "calendar_update",
            calendar_event_preview("Update event", preview_event, event_id),
        )
        if decision == "deny" or cancellation_event.is_set():
            emit_step(str(uuid.uuid4()), "warning", f"Cancelled Calendar update for '{current['summary']}'")
            return "The user cancelled the Calendar update. No changes were made."

        update_step_id = str(uuid.uuid4())
        emit_step(update_step_id, "running", "Updating Google Calendar event", current["summary"])
        try:
            ensure_turn_active()
            updated = service.events().patch(
                calendarId="primary",
                eventId=event_id,
                body=patch,
                sendUpdates="all",
            ).execute()
        except (HttpError, HTTPException) as error:
            return calendar_tool_error(update_step_id, "Could not update Calendar event", error)
        updated_event = public_calendar_event(updated)
        emit_step(update_step_id, "completed", f"Updated Calendar event: {updated_event['summary']}")
        return f"Updated Calendar event '{updated_event['summary']}' and notified its guests."

    def delete_calendar_event(event_id: str) -> str:
        """Cancels an existing Calendar event after showing the user a confirmation preview.

        Args:
            event_id: The ID returned by get_calendar_events; never guess this value.
        """
        lookup_step_id = str(uuid.uuid4())
        emit_step(lookup_step_id, "running", "Loading Calendar event to cancel", event_id)
        try:
            service = calendar_service()
            current = public_calendar_event(
                service.events().get(calendarId="primary", eventId=event_id).execute()
            )
        except (HttpError, HTTPException) as error:
            return calendar_tool_error(lookup_step_id, "Could not find Calendar event to cancel", error)

        emit_step(lookup_step_id, "completed", "Calendar event ready for cancellation", current["summary"])
        decision = request_calendar_approval(
            "calendar_delete",
            calendar_event_preview("Cancel event", current, event_id),
        )
        if decision == "deny" or cancellation_event.is_set():
            emit_step(str(uuid.uuid4()), "warning", f"Cancelled request to delete '{current['summary']}'")
            return "The user cancelled the Calendar cancellation. The event remains scheduled."

        delete_step_id = str(uuid.uuid4())
        emit_step(delete_step_id, "running", "Cancelling Google Calendar event", current["summary"])
        try:
            ensure_turn_active()
            service.events().delete(
                calendarId="primary",
                eventId=event_id,
                sendUpdates="all",
            ).execute()
        except (HttpError, HTTPException) as error:
            return calendar_tool_error(delete_step_id, "Could not cancel Calendar event", error)
        emit_step(delete_step_id, "completed", f"Cancelled Calendar event: {current['summary']}")
        return f"Cancelled Calendar event '{current['summary']}' and notified its guests."

    def sheets_clients():
        active_email, credentials = get_credentials_for_active_user()
        if not active_email or not credentials:
            raise HTTPException(status_code=401, detail="Sign in with Google to use Sheets.")
        return (
            build("sheets", "v4", credentials=credentials),
            build("drive", "v3", credentials=credentials),
        )

    def sheets_error_detail(error: Exception) -> str:
        if isinstance(error, HttpError):
            status_code = int(getattr(error.resp, "status", 502))
            if status_code == 403:
                detail = "Google Sheets or Drive access was denied. Reconnect Google and grant the requested permission."
            elif status_code == 404:
                detail = "The spreadsheet or requested item was not found."
            else:
                detail = f"Google Sheets could not complete the request (HTTP {status_code})."
        elif isinstance(error, HTTPException):
            detail = str(error.detail)
        elif isinstance(error, ValueError):
            detail = str(error)
        else:
            raise error
        return detail

    def sheets_tool_error(step_id: str, label: str, error: Exception) -> str:
        detail = sheets_error_detail(error)
        emit_step(step_id, "warning", label, detail)
        return f"{label}: {detail}"

    def spreadsheet_metadata(sheets_service, spreadsheet_id: str) -> dict:
        return sheets_service.spreadsheets().get(
            spreadsheetId=spreadsheet_id,
            fields="spreadsheetId,properties(title),sheets(properties(sheetId,title,index,gridProperties))",
        ).execute()

    def find_sheet_tab(metadata: dict, sheet_name: str = "") -> dict:
        tabs = [item["properties"] for item in metadata.get("sheets", [])]
        if sheet_name:
            for tab in tabs:
                if tab.get("title") == sheet_name:
                    return tab
            raise HTTPException(status_code=404, detail=f"Worksheet '{sheet_name}' was not found.")
        if not tabs:
            raise HTTPException(status_code=404, detail="The spreadsheet has no worksheets.")
        return tabs[0]

    def resolve_sheet_range(range_name: str, default_sheet: str = "") -> str:
        if not range_name:
            if not default_sheet:
                raise HTTPException(status_code=422, detail="Provide an A1 range such as Sheet1!A1:D20.")
            return quoted_sheet_range(default_sheet)
        parsed_sheet, _, _, _, _ = parse_a1_range(range_name)
        coordinates = range_name.rsplit("!", 1)[-1]
        if parsed_sheet:
            return quoted_sheet_range(parsed_sheet, coordinates)
        if default_sheet:
            return quoted_sheet_range(default_sheet, coordinates)
        return range_name

    def spreadsheet_link(spreadsheet_id: str, drive_service=None) -> str:
        if drive_service:
            file_info = drive_service.files().get(
                fileId=spreadsheet_id,
                fields="id,name,webViewLink,mimeType",
            ).execute()
            return file_info.get("webViewLink") or f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/edit"
        return f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/edit"

    def spreadsheet_export_bytes(
        spreadsheet_id: str,
        format_name: str,
        sheet_name: str = "",
    ) -> tuple[bytes, str, str, bool, str]:
        sheets_service, drive_service = sheets_clients()
        metadata = spreadsheet_metadata(sheets_service, spreadsheet_id)
        title = metadata.get("properties", {}).get("title", "spreadsheet")
        tabs = [item["properties"]["title"] for item in metadata.get("sheets", [])]
        normalized = format_name.upper().replace(" ", "")
        aliases = {
            "EXCEL": "XLSX",
            "WORD": "DOCX",
            "DOC": "DOCX",
            "POWERPOINT": "PPTX",
            "PPT": "PPTX",
        }
        normalized = aliases.get(normalized, normalized)
        extensions = {
            "XLSX": ("xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
            "CSV": ("csv", "text/csv"),
            "PDF": ("pdf", "application/pdf"),
            "DOCX": ("docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
            "PPTX": ("pptx", "application/vnd.openxmlformats-officedocument.presentationml.presentation"),
        }
        if normalized not in extensions:
            raise HTTPException(status_code=422, detail="Choose XLSX, CSV, PDF, Word (DOCX), or PowerPoint (PPTX).")

        extension, content_type = extensions[normalized]
        if normalized in {"XLSX", "PDF"}:
            content = drive_service.files().export(
                fileId=spreadsheet_id,
                mimeType=content_type,
            ).execute()
            if isinstance(content, str):
                content = content.encode("utf-8")
            return content, sanitize_download_filename(title, extension), content_type, False, title

        if not tabs:
            raise HTTPException(status_code=404, detail="The spreadsheet has no worksheets to export.")
        selected_tab = sheet_name or tabs[0]
        if selected_tab not in tabs:
            raise HTTPException(status_code=404, detail=f"Worksheet '{selected_tab}' was not found.")
        if normalized == "CSV":
            range_name = quoted_sheet_range(selected_tab, "A1:AZ10000")
        else:
            range_name = quoted_sheet_range(selected_tab, "A1:Z1000")
        values_result = sheets_service.spreadsheets().values().get(
            spreadsheetId=spreadsheet_id,
            range=range_name,
            valueRenderOption="FORMATTED_VALUE",
        ).execute()
        values = values_result.get("values", [])
        if normalized == "CSV":
            truncated = len(values) >= 10000 or max((len(row) for row in values), default=0) >= 52
            content = csv_export(values)
        else:
            content, _, truncated = office_export(normalized, title, selected_tab, values)
        return content, sanitize_download_filename(f"{title}_{selected_tab}", extension), content_type, truncated, title

    def request_sheets_approval(action_type: str, preview: dict) -> str:
        return request_user_approval_sync(
            event_queue=event_queue,
            loop=loop,
            user_email=user_email,
            turn_record=turn_record,
            action_type=action_type,
            recipient="",
            preview_data=preview,
            cancellation_event=cancellation_event,
        )

    def find_spreadsheets(query: str = "", max_results: int = 10) -> list[dict] | str:
        """Finds the user's Google Sheets files by title, returning IDs and links.

        Args:
            query: Optional words from a spreadsheet title.
            max_results: Maximum files to return, from 1 to 25.
        """
        step_id = str(uuid.uuid4())
        emit_step(step_id, "running", "Searching Google Sheets", query.strip() or "Recent spreadsheets")
        try:
            _, drive_service = sheets_clients()
            escaped_query = query.strip().replace("\\", "\\\\").replace("'", "\\'")
            q = "mimeType = 'application/vnd.google-apps.spreadsheet' and trashed = false"
            if escaped_query:
                q += f" and name contains '{escaped_query}'"
            response = drive_service.files().list(
                q=q,
                pageSize=min(max(int(max_results), 1), 25),
                orderBy="modifiedTime desc",
                fields="files(id,name,mimeType,modifiedTime,webViewLink)",
            ).execute()
        except (HttpError, HTTPException, ValueError) as error:
            return sheets_tool_error(step_id, "Could not search Google Sheets", error)
        files = response.get("files", [])
        emit_step(step_id, "completed", f"Found {len(files)} spreadsheet(s)", query.strip() or "Recent spreadsheets")
        return files

    def get_spreadsheet_info(spreadsheet_id: str) -> dict | str:
        """Returns a spreadsheet's title, link, and worksheet tabs.

        Args:
            spreadsheet_id: The ID returned by find_spreadsheets.
        """
        step_id = str(uuid.uuid4())
        emit_step(step_id, "running", "Opening Google spreadsheet", spreadsheet_id)
        try:
            sheets_service, drive_service = sheets_clients()
            metadata = spreadsheet_metadata(sheets_service, spreadsheet_id)
            link = spreadsheet_link(spreadsheet_id, drive_service)
        except (HttpError, HTTPException, ValueError) as error:
            return sheets_tool_error(step_id, "Could not open spreadsheet", error)
        result = {
            "spreadsheet_id": spreadsheet_id,
            "title": metadata.get("properties", {}).get("title", "Untitled spreadsheet"),
            "url": link,
            "sheets": [
                {
                    "sheet_id": item["properties"]["sheetId"],
                    "title": item["properties"]["title"],
                }
                for item in metadata.get("sheets", [])
            ],
        }
        emit_step(step_id, "completed", f"Opened spreadsheet: {result['title']}")
        return result

    def read_spreadsheet_range(spreadsheet_id: str, range_name: str = "") -> dict | str:
        """Reads spreadsheet cells using A1 notation, such as 'Budget!A1:D25'.

        Args:
            spreadsheet_id: The ID returned by find_spreadsheets.
            range_name: Optional A1 range; defaults to the first worksheet's A1:Z50.
        """
        step_id = str(uuid.uuid4())
        emit_step(step_id, "running", "Reading spreadsheet cells", range_name or "First worksheet, A1:Z50")
        try:
            sheets_service, _ = sheets_clients()
            metadata = spreadsheet_metadata(sheets_service, spreadsheet_id)
            tab = find_sheet_tab(metadata)
            actual_range = (
                resolve_sheet_range(range_name, tab["title"])
                if range_name
                else quoted_sheet_range(tab["title"], "A1:Z50")
            )
            result = sheets_service.spreadsheets().values().get(
                spreadsheetId=spreadsheet_id,
                range=actual_range,
                valueRenderOption="FORMATTED_VALUE",
            ).execute()
        except (HttpError, HTTPException, ValueError) as error:
            return sheets_tool_error(step_id, "Could not read spreadsheet cells", error)
        values = result.get("values", [])
        truncated = len(values) > 200
        values = values[:200]
        cells_remaining = 10000
        limited_values = []
        for row in values:
            if cells_remaining <= 0:
                truncated = True
                break
            limited_values.append(row[:cells_remaining])
            if len(row) > cells_remaining:
                truncated = True
            cells_remaining -= min(len(row), cells_remaining)
        emit_step(step_id, "completed", f"Read {sum(len(row) for row in limited_values)} cell(s)", actual_range)
        return {"range": actual_range, "values": limited_values, "truncated": truncated}

    def create_spreadsheet(
        title: str,
        sheet_name: str = "Overview",
        headers: Optional[list[str]] = None,
        rows: Optional[list[list[str]]] = None,
    ) -> dict | str:
        """Creates a professional Google spreadsheet, optionally with headers and data.

        Args:
            title: Spreadsheet title.
            sheet_name: Name of its first worksheet tab.
            headers: Optional column headings for the first row.
            rows: Optional rows of values. Use strings; formulas beginning with '=' are evaluated.
        """
        step_id = str(uuid.uuid4())
        emit_step(step_id, "running", "Creating Google spreadsheet", title)
        if not title.strip() or not sheet_name.strip():
            return sheets_tool_error(step_id, "Could not create spreadsheet", HTTPException(status_code=422, detail="Spreadsheet and worksheet titles cannot be empty."))
        try:
            sheets_service, _ = sheets_clients()
            ensure_turn_active()
            created = sheets_service.spreadsheets().create(
                body={
                    "properties": {"title": title.strip()},
                    "sheets": [{"properties": {"title": sheet_name.strip()}}],
                },
                fields="spreadsheetId,spreadsheetUrl,sheets(properties(sheetId,title,gridProperties))",
            ).execute()
            spreadsheet_id = created["spreadsheetId"]
            sheet_id = created["sheets"][0]["properties"]["sheetId"]
            data = ([headers] if headers else []) + (rows or [])
            requests = []
            if data:
                ensure_turn_active()
                sheets_service.spreadsheets().values().update(
                    spreadsheetId=spreadsheet_id,
                    range=quoted_sheet_range(sheet_name.strip(), "A1"),
                    valueInputOption="USER_ENTERED",
                    body={"values": data},
                ).execute()
            if headers:
                requests = professional_format_requests(sheet_id, len(headers))
            else:
                requests = [
                    {
                        "autoResizeDimensions": {
                            "dimensions": {
                                "sheetId": sheet_id,
                                "dimension": "COLUMNS",
                                "startIndex": 0,
                                "endIndex": max((len(row) for row in data), default=1),
                            }
                        }
                    }
                ]
            ensure_turn_active()
            sheets_service.spreadsheets().batchUpdate(
                spreadsheetId=spreadsheet_id,
                body={"requests": requests},
            ).execute()
        except (HttpError, HTTPException, ValueError, KeyError) as error:
            return sheets_tool_error(step_id, "Could not create spreadsheet", error)
        link = created.get("spreadsheetUrl") or f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/edit"
        emit_step(step_id, "completed", f"Created spreadsheet: {title.strip()}", link)
        return {"spreadsheet_id": spreadsheet_id, "title": title.strip(), "url": link}

    def write_sheet_range(
        spreadsheet_id: str,
        range_name: str,
        action: str = "update",
        values: Optional[list[list[str]]] = None,
    ) -> str:
        """Updates, appends, or clears spreadsheet cells; formulas are accepted as values beginning with '='.

        Args:
            spreadsheet_id: The ID returned by find_spreadsheets.
            range_name: A1 range such as 'Expenses!A2:C10'.
            action: One of 'update', 'append', or 'clear'.
            values: Rows and columns to write; required for update and append.
        """
        step_id = str(uuid.uuid4())
        emit_step(step_id, "running", f"Applying spreadsheet '{action}'", range_name)
        action = action.strip().lower()
        try:
            if action not in {"update", "append", "clear"}:
                raise HTTPException(status_code=422, detail="Action must be update, append, or clear.")
            sheets_service, _ = sheets_clients()
            parsed_sheet, _, _, _, _ = parse_a1_range(range_name)
            metadata = spreadsheet_metadata(sheets_service, spreadsheet_id)
            tab = find_sheet_tab(metadata, parsed_sheet)
            actual_range = resolve_sheet_range(range_name, tab["title"])
            if action == "clear":
                ensure_turn_active()
                sheets_service.spreadsheets().values().clear(
                    spreadsheetId=spreadsheet_id,
                    range=actual_range,
                    body={},
                ).execute()
            else:
                if not values:
                    raise HTTPException(status_code=422, detail="Provide cell values for update or append.")
                if action == "update":
                    ensure_turn_active()
                    result = sheets_service.spreadsheets().values().update(
                        spreadsheetId=spreadsheet_id,
                        range=actual_range,
                        valueInputOption="USER_ENTERED",
                        body={"values": values},
                    ).execute()
                else:
                    ensure_turn_active()
                    result = sheets_service.spreadsheets().values().append(
                        spreadsheetId=spreadsheet_id,
                        range=actual_range,
                        valueInputOption="USER_ENTERED",
                        insertDataOption="INSERT_ROWS",
                        body={"values": values},
                    ).execute()
        except (HttpError, HTTPException, ValueError) as error:
            return sheets_tool_error(step_id, "Could not update spreadsheet", error)
        changed = result.get("updatedCells", 0) if action != "clear" else "selected"
        emit_step(step_id, "completed", f"Spreadsheet {action} completed", f"{changed} cell(s)")
        return f"Spreadsheet {action} completed for {actual_range}."

    def manage_sheet_tabs(
        spreadsheet_id: str,
        action: str,
        sheet_name: str,
        new_name: str = "",
    ) -> str:
        """Adds, renames, or deletes a worksheet tab.

        Args:
            spreadsheet_id: The spreadsheet ID.
            action: One of 'add', 'rename', or 'delete'.
            sheet_name: Existing tab name for rename/delete, or desired new tab name for add.
            new_name: Required target name for rename.
        """
        step_id = str(uuid.uuid4())
        emit_step(step_id, "running", f"Managing worksheet tab ({action})", sheet_name)
        try:
            action = action.strip().lower()
            if action not in {"add", "rename", "delete"}:
                raise HTTPException(status_code=422, detail="Action must be add, rename, or delete.")
            if not sheet_name.strip():
                raise HTTPException(status_code=422, detail="Worksheet name cannot be empty.")
            sheets_service, _ = sheets_clients()
            metadata = spreadsheet_metadata(sheets_service, spreadsheet_id)
            if action == "add":
                requests = [{"addSheet": {"properties": {"title": sheet_name.strip()}}}]
            else:
                tab = find_sheet_tab(metadata, sheet_name)
                if action == "rename":
                    if not new_name.strip():
                        raise HTTPException(status_code=422, detail="Provide a new worksheet name.")
                    requests = [{
                        "updateSheetProperties": {
                            "properties": {"sheetId": tab["sheetId"], "title": new_name.strip()},
                            "fields": "title",
                        }
                    }]
                else:
                    if len(metadata.get("sheets", [])) <= 1:
                        raise HTTPException(status_code=422, detail="A spreadsheet must keep at least one worksheet.")
                    requests = [{"deleteSheet": {"sheetId": tab["sheetId"]}}]
            ensure_turn_active()
            sheets_service.spreadsheets().batchUpdate(
                spreadsheetId=spreadsheet_id,
                body={"requests": requests},
            ).execute()
        except (HttpError, HTTPException, ValueError) as error:
            return sheets_tool_error(step_id, "Could not manage worksheet tab", error)
        emit_step(step_id, "completed", f"Worksheet tab {action} completed", sheet_name)
        return f"Worksheet tab {action} completed."

    def format_spreadsheet(
        spreadsheet_id: str,
        sheet_name: str,
        format_type: str = "professional",
        range_name: str = "",
    ) -> str:
        """Applies professional header styling or a number format to a cell range.

        Args:
            spreadsheet_id: The spreadsheet ID.
            sheet_name: Worksheet tab to format.
            format_type: 'professional', 'currency', 'percentage', 'date', 'number', or 'text'.
            range_name: Required A1 range for number formats, such as 'B2:B50'.
        """
        step_id = str(uuid.uuid4())
        emit_step(step_id, "running", "Formatting spreadsheet", f"{sheet_name} · {format_type}")
        formats = {
            "currency": {"type": "CURRENCY", "pattern": "$#,##0.00"},
            "percentage": {"type": "PERCENT", "pattern": "0.00%"},
            "date": {"type": "DATE", "pattern": "mmm d, yyyy"},
            "number": {"type": "NUMBER", "pattern": "#,##0.00"},
            "text": {"type": "TEXT", "pattern": "@"},
        }
        try:
            sheets_service, _ = sheets_clients()
            metadata = spreadsheet_metadata(sheets_service, spreadsheet_id)
            tab = find_sheet_tab(metadata, sheet_name)
            format_type = format_type.strip().lower()
            if format_type == "professional":
                column_count = tab.get("gridProperties", {}).get("columnCount", 1)
                requests = professional_format_requests(tab["sheetId"], column_count)
            elif format_type in formats:
                if not range_name:
                    raise HTTPException(status_code=422, detail="Provide a cell range for number formatting.")
                parsed_tab, cell_range = grid_range(range_name, tab["sheetId"])
                if parsed_tab and parsed_tab != sheet_name:
                    raise HTTPException(status_code=422, detail="The A1 range must belong to the selected worksheet.")
                requests = [{
                    "repeatCell": {
                        "range": cell_range,
                        "cell": {"userEnteredFormat": {"numberFormat": formats[format_type]}},
                        "fields": "userEnteredFormat.numberFormat",
                    }
                }]
            else:
                raise HTTPException(status_code=422, detail="Unsupported spreadsheet format type.")
            ensure_turn_active()
            sheets_service.spreadsheets().batchUpdate(
                spreadsheetId=spreadsheet_id,
                body={"requests": requests},
            ).execute()
        except (HttpError, HTTPException, ValueError) as error:
            return sheets_tool_error(step_id, "Could not format spreadsheet", error)
        emit_step(step_id, "completed", "Spreadsheet formatting completed", sheet_name)
        return f"Applied {format_type} formatting to '{sheet_name}'."

    def sort_sheet_range(
        spreadsheet_id: str,
        range_name: str,
        sort_column: str,
        ascending: bool = True,
        has_header: bool = True,
    ) -> str:
        """Sorts a range by a column letter, such as sorting A1:D50 by column C.

        Args:
            spreadsheet_id: The spreadsheet ID.
            range_name: A1 range to sort, including headers if present.
            sort_column: Column letter used to sort, such as 'A' or 'C'.
            ascending: True for ascending order, false for descending.
            has_header: True when the first row contains column headings; the header is kept in place.
        """
        step_id = str(uuid.uuid4())
        emit_step(step_id, "running", "Sorting spreadsheet range", range_name)
        try:
            parsed_tab, _, _, start_column, end_column = parse_a1_range(range_name)
            sort_column_index = parse_a1_range(f"{sort_column.strip()}1")[3]
            if not start_column <= sort_column_index < end_column:
                raise HTTPException(status_code=422, detail="Sort column must be inside the selected range.")
            sheets_service, _ = sheets_clients()
            metadata = spreadsheet_metadata(sheets_service, spreadsheet_id)
            tab = find_sheet_tab(metadata, parsed_tab)
            _, target_range = grid_range(range_name, tab["sheetId"])
            if has_header:
                if target_range["endRowIndex"] - target_range["startRowIndex"] <= 1:
                    raise HTTPException(status_code=422, detail="A range needs at least one data row below its header.")
                target_range["startRowIndex"] += 1
            ensure_turn_active()
            sheets_service.spreadsheets().batchUpdate(
                spreadsheetId=spreadsheet_id,
                body={"requests": [{
                    "sortRange": {
                        "range": target_range,
                        "sortSpecs": [{
                            "dimensionIndex": sort_column_index,
                            "sortOrder": "ASCENDING" if ascending else "DESCENDING",
                        }],
                    }
                }]},
            ).execute()
        except (HttpError, HTTPException, ValueError) as error:
            return sheets_tool_error(step_id, "Could not sort spreadsheet range", error)
        emit_step(step_id, "completed", "Spreadsheet range sorted", f"Column {sort_column.upper()}")
        return f"Sorted {range_name} by column {sort_column.upper()}."

    def set_sheet_filter(spreadsheet_id: str, range_name: str, enabled: bool = True) -> str:
        """Adds or removes a basic filter for a spreadsheet range.

        Args:
            spreadsheet_id: The spreadsheet ID.
            range_name: A1 range including the header row when enabling a filter.
            enabled: True to enable filtering, false to remove the sheet's basic filter.
        """
        step_id = str(uuid.uuid4())
        emit_step(step_id, "running", "Updating spreadsheet filter", range_name)
        try:
            parsed_tab, _, _, _, _ = parse_a1_range(range_name)
            sheets_service, _ = sheets_clients()
            metadata = spreadsheet_metadata(sheets_service, spreadsheet_id)
            tab = find_sheet_tab(metadata, parsed_tab)
            _, target_range = grid_range(range_name, tab["sheetId"])
            request = (
                {"setBasicFilter": {"filter": {"range": target_range}}}
                if enabled
                else {"clearBasicFilter": {"sheetId": tab["sheetId"]}}
            )
            ensure_turn_active()
            sheets_service.spreadsheets().batchUpdate(
                spreadsheetId=spreadsheet_id,
                body={"requests": [request]},
            ).execute()
        except (HttpError, HTTPException, ValueError) as error:
            return sheets_tool_error(step_id, "Could not update spreadsheet filter", error)
        emit_step(step_id, "completed", f"Spreadsheet filter {'enabled' if enabled else 'removed'}", range_name)
        return f"Spreadsheet filter {'enabled' if enabled else 'removed'}."

    def create_sheet_chart(
        spreadsheet_id: str,
        range_name: str,
        chart_type: str,
        title: str,
        domain_column: str = "",
        series_columns: Optional[list[str]] = None,
    ) -> str:
        """Creates a chart from an A1 data range.

        Args:
            spreadsheet_id: The spreadsheet ID.
            range_name: A1 range with a header row, such as 'Data!A1:C20'.
            chart_type: One of 'COLUMN', 'BAR', 'LINE', 'AREA', or 'SCATTER'.
            title: Chart title.
            domain_column: Column letter containing labels for the horizontal axis; defaults to the first range column.
            series_columns: Optional numeric data column letters; defaults to all other selected columns.
        """
        step_id = str(uuid.uuid4())
        emit_step(step_id, "running", "Creating spreadsheet chart", title)
        try:
            parsed_tab, _, _, start_column, end_column = parse_a1_range(range_name)
            _, target_range = grid_range(range_name, 0)
            domain_index = parse_a1_range(f"{domain_column.strip()}1")[3] if domain_column.strip() else start_column
            if not start_column <= domain_index < end_column:
                raise HTTPException(status_code=422, detail="The chart label column must be inside the data range.")
            chart_type = chart_type.strip().upper()
            if chart_type not in {"COLUMN", "BAR", "LINE", "AREA", "SCATTER"}:
                raise HTTPException(status_code=422, detail="Chart type must be COLUMN, BAR, LINE, AREA, or SCATTER.")
            selected_series = (
                [parse_a1_range(f"{column.strip()}1")[3] for column in series_columns]
                if series_columns
                else [column for column in range(start_column, end_column) if column != domain_index]
            )
            if not selected_series or any(not start_column <= column < end_column for column in selected_series):
                raise HTTPException(status_code=422, detail="Choose at least one chart data column inside the range.")
            sheets_service, _ = sheets_clients()
            metadata = spreadsheet_metadata(sheets_service, spreadsheet_id)
            tab = find_sheet_tab(metadata, parsed_tab)
            target_range["sheetId"] = tab["sheetId"]
            domain_range = dict(target_range, startColumnIndex=domain_index, endColumnIndex=domain_index + 1)
            series = []
            for column in selected_series:
                series_range = dict(target_range, startColumnIndex=column, endColumnIndex=column + 1)
                series.append({"series": {"sourceRange": {"sources": [series_range]}}})
            chart = {
                "spec": {
                    "title": title,
                    "basicChart": {
                        "chartType": chart_type,
                        "legendPosition": "BOTTOM_LEGEND",
                        "headerCount": 1,
                        "domains": [{"domain": {"sourceRange": {"sources": [domain_range]}}}],
                        "series": series,
                    },
                },
                "position": {
                    "overlayPosition": {
                        "anchorCell": {
                            "sheetId": tab["sheetId"],
                            "rowIndex": target_range["endRowIndex"] + 1,
                            "columnIndex": target_range["endColumnIndex"] + 1,
                        },
                        "widthPixels": 640,
                        "heightPixels": 400,
                    }
                },
            }
            ensure_turn_active()
            sheets_service.spreadsheets().batchUpdate(
                spreadsheetId=spreadsheet_id,
                body={"requests": [{"addChart": {"chart": chart}}]},
            ).execute()
        except (HttpError, HTTPException, ValueError) as error:
            return sheets_tool_error(step_id, "Could not create spreadsheet chart", error)
        emit_step(step_id, "completed", f"Created {chart_type} chart", title)
        return f"Created {chart_type} chart '{title}'."

    def store_sheet_download(content: bytes, filename: str, content_type: str) -> str:
        if len(content) > MAX_SHEETS_DOWNLOAD_BYTES:
            raise HTTPException(
                status_code=413,
                detail="This export exceeds the 25 MB chat-download limit. Download it from Google Drive instead.",
            )
        now = time.time()
        with SHEETS_DOWNLOAD_LOCK:
            expired_ids = [
                download_id
                for download_id, item in SHEETS_DOWNLOADS.items()
                if item["expires_at"] <= now
            ]
            for expired_id in expired_ids:
                SHEETS_DOWNLOADS.pop(expired_id, None)
            while (
                len(SHEETS_DOWNLOADS) >= MAX_SHEETS_DOWNLOADS
                or sum(item["size"] for item in SHEETS_DOWNLOADS.values()) + len(content) > MAX_SHEETS_DOWNLOAD_STORAGE_BYTES
            ):
                oldest_id = min(SHEETS_DOWNLOADS, key=lambda key: SHEETS_DOWNLOADS[key]["expires_at"])
                SHEETS_DOWNLOADS.pop(oldest_id)
            download_id = uuid.uuid4().hex
            SHEETS_DOWNLOADS[download_id] = {
                "user_email": user_email.lower(),
                "content": content,
                "content_type": content_type,
                "filename": filename,
                "size": len(content),
                "expires_at": now + SHEETS_DOWNLOAD_TTL_SECONDS,
            }
        return f"/api/sheets/download/{download_id}"

    def download_spreadsheet(
        spreadsheet_id: str,
        format_name: str = "",
        sheet_name: str = "",
    ) -> str:
        """Exports a spreadsheet as XLSX, CSV, PDF, DOCX (Word), or PPTX (PowerPoint).

        Args:
            spreadsheet_id: The spreadsheet ID returned by find_spreadsheets.
            format_name: Required output format: XLSX, CSV, PDF, DOCX, or PPTX. Ask if the user has not chosen one.
            sheet_name: Worksheet tab for CSV, DOCX, or PPTX; XLSX and PDF export the whole spreadsheet.
        """
        step_id = str(uuid.uuid4())
        if not format_name.strip():
            emit_step(step_id, "completed", "Choose a spreadsheet download format")
            return "Ask the user which format they want: XLSX, CSV, PDF, Word (DOCX), or PowerPoint (PPTX)."
        emit_step(step_id, "running", "Preparing spreadsheet download", format_name.upper())
        try:
            content, filename, content_type, truncated, title = spreadsheet_export_bytes(
                spreadsheet_id,
                format_name,
                sheet_name,
            )
            ensure_turn_active()
            link = store_sheet_download(content, filename, content_type)
        except (HttpError, HTTPException, ValueError, KeyError) as error:
            return sheets_tool_error(step_id, "Could not export spreadsheet", error)
        detail = "The file includes a documented content limit." if truncated else filename
        emit_step(step_id, "completed", f"Prepared {format_name.upper()} download", detail)
        note = " This export has documented row and column limits." if truncated else ""
        open_link = f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/edit"
        return (
            f"Prepared {format_name.upper()} export for {title}.{note} "
            f"The download link expires in 30 minutes. "
            f"[Download]({link}) [Open]({open_link})"
        )

    def share_spreadsheet(
        spreadsheet_id: str,
        email_addresses: list[str],
        role: str = "reader",
        delivery: str = "link",
        attachment_format: str = "",
    ) -> str:
        """Grants named people access to a spreadsheet, optionally emailing a link or attachment after approval.

        Args:
            spreadsheet_id: The spreadsheet ID returned by find_spreadsheets.
            email_addresses: Specific email addresses to share with; public link sharing is not supported.
            role: Permission role: 'reader', 'commenter', or 'writer'.
            delivery: 'link' for a Google Drive invite, 'attachment' for an email attachment, or 'both'.
            attachment_format: Required for attachment/both: XLSX, CSV, PDF, DOCX, or PPTX.
        """
        step_id = str(uuid.uuid4())
        emit_step(step_id, "running", "Preparing spreadsheet sharing request", spreadsheet_id)
        role = role.strip().lower()
        role_map = {"reader": "reader", "viewer": "reader", "commenter": "commenter", "writer": "writer", "editor": "writer"}
        delivery = delivery.strip().lower()
        clean_emails = []
        for raw_email in email_addresses:
            email = parseaddr(raw_email.strip())[1]
            if (
                not email
                or not re.fullmatch(r"[^@\s,;<>]+@[^@\s,;<>]+\.[^@\s,;<>]+", email)
            ):
                return sheets_tool_error(step_id, "Could not share spreadsheet", HTTPException(status_code=422, detail=f"Invalid email address: {raw_email}"))
            if email.casefold() not in {item.casefold() for item in clean_emails}:
                clean_emails.append(email)
        if not clean_emails or len(clean_emails) > 20:
            return sheets_tool_error(step_id, "Could not share spreadsheet", HTTPException(status_code=422, detail="Provide between 1 and 20 named email addresses."))
        if role not in role_map or delivery not in {"link", "attachment", "both"}:
            return sheets_tool_error(step_id, "Could not share spreadsheet", HTTPException(status_code=422, detail="Choose a valid permission role and delivery method."))
        needs_attachment = delivery in {"attachment", "both"}
        if needs_attachment and not attachment_format.strip():
            emit_step(step_id, "completed", "Choose an email attachment format")
            return "Ask the user whether the email attachment should be XLSX, CSV, PDF, Word (DOCX), or PowerPoint (PPTX)."

        try:
            sheets_service, drive_service = sheets_clients()
            metadata = spreadsheet_metadata(sheets_service, spreadsheet_id)
            title = metadata.get("properties", {}).get("title", "Untitled spreadsheet")
            link = spreadsheet_link(spreadsheet_id, drive_service)
            attachment = None
            if needs_attachment:
                attachment = spreadsheet_export_bytes(spreadsheet_id, attachment_format, "")[:4]
        except (HttpError, HTTPException, ValueError, KeyError) as error:
            return sheets_tool_error(step_id, "Could not prepare spreadsheet share", error)

        preview = {
            "action": "Share spreadsheet",
            "spreadsheet_title": title,
            "spreadsheet_url": link,
            "email_addresses": clean_emails,
            "role": role_map[role],
            "delivery": delivery,
            "attachment_format": attachment_format.upper() if needs_attachment else "",
        }
        decision = request_sheets_approval("sheets_share", preview)
        if decision == "deny" or cancellation_event.is_set():
            emit_step(str(uuid.uuid4()), "warning", f"Cancelled sharing '{title}'")
            return "The user cancelled spreadsheet sharing. No permissions were changed."

        share_step_id = str(uuid.uuid4())
        emit_step(share_step_id, "running", "Granting spreadsheet permissions", ", ".join(clean_emails))
        granted_emails = []
        for email in clean_emails:
            try:
                ensure_turn_active()
                drive_service.permissions().create(
                    fileId=spreadsheet_id,
                    body={"type": "user", "role": role_map[role], "emailAddress": email},
                    sendNotificationEmail=delivery == "link",
                    fields="id,emailAddress,role",
                ).execute()
            except (HttpError, HTTPException) as error:
                detail = sheets_error_detail(error)
                emit_step(share_step_id, "warning", "Could not grant all spreadsheet permissions", detail)
                granted_text = f" Access was already granted to: {', '.join(granted_emails)}." if granted_emails else ""
                return f"Could not grant spreadsheet permission to {email}: {detail}.{granted_text} No email was sent."
            granted_emails.append(email)

        if needs_attachment:
            file_content, filename, content_type, truncated = attachment
            main_type, sub_type = content_type.split("/", 1)
            body_text = f"{display_name} shared '{title}' with you."
            if delivery == "both":
                body_text += f"\n\nOpen the Google spreadsheet: {link}"
            sent_emails = []
            failed_emails = []
            for email in clean_emails:
                try:
                    ensure_turn_active()
                    message = EmailMessage()
                    _set_sender_headers(message, display_name, user_email)
                    message["To"] = email
                    message["Subject"] = f"{title} shared with you"
                    message.set_content(body_text)
                    message.add_attachment(file_content, maintype=main_type, subtype=sub_type, filename=filename)
                    raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
                    gmail_service.users().messages().send(userId="me", body={"raw": raw}).execute()
                    sent_emails.append(email)
                except (HttpError, HTTPException, ValueError) as error:
                    failed_emails.append(f"{email}: {sheets_error_detail(error)}")
            if failed_emails:
                detail = "; ".join(failed_emails)
                emit_step(
                    share_step_id,
                    "warning",
                    "Spreadsheet permissions were granted, but email delivery was incomplete",
                    detail,
                )
                sent_text = f" Email was sent to {', '.join(sent_emails)}." if sent_emails else ""
                return f"Spreadsheet access was granted to {', '.join(granted_emails)}.{sent_text} Email delivery failed for: {detail}."
        emit_step(share_step_id, "completed", f"Shared spreadsheet '{title}'", ", ".join(clean_emails))
        if needs_attachment and truncated:
            return f"Granted {role_map[role]} access and emailed the requested export to {', '.join(clean_emails)}. The attachment has documented row/column limits."
        return f"Granted {role_map[role]} access to '{title}' for {', '.join(clean_emails)}."

    def list_spreadsheet_permissions(spreadsheet_id: str) -> list[dict] | str:
        """Lists named user permissions for a spreadsheet without changing them.

        Args:
            spreadsheet_id: The spreadsheet ID.
        """
        step_id = str(uuid.uuid4())
        emit_step(step_id, "running", "Checking spreadsheet permissions", spreadsheet_id)
        try:
            _, drive_service = sheets_clients()
            permissions = drive_service.permissions().list(
                fileId=spreadsheet_id,
                fields="permissions(id,type,emailAddress,role,displayName)",
                supportsAllDrives=True,
            ).execute().get("permissions", [])
        except (HttpError, HTTPException, ValueError) as error:
            return sheets_tool_error(step_id, "Could not list spreadsheet permissions", error)
        named = [
            {"email": item.get("emailAddress", ""), "name": item.get("displayName", ""), "role": item.get("role", "")}
            for item in permissions
            if item.get("type") == "user" and item.get("emailAddress")
        ]
        emit_step(step_id, "completed", f"Found {len(named)} named spreadsheet permission(s)")
        return named

    def manage_spreadsheet_permission(
        spreadsheet_id: str,
        email_address: str,
        action: str,
        role: str = "reader",
    ) -> str:
        """Changes or removes a named user's permission after approval.

        Args:
            spreadsheet_id: The spreadsheet ID.
            email_address: Exact email address whose access should change.
            action: 'change' to change role or 'remove' to revoke access.
            role: New role for a change: 'reader', 'commenter', or 'writer'.
        """
        step_id = str(uuid.uuid4())
        action = action.strip().lower()
        email = parseaddr(email_address.strip())[1]
        role_map = {"reader": "reader", "viewer": "reader", "commenter": "commenter", "writer": "writer", "editor": "writer"}
        if not email or not re.fullmatch(r"[^@\s,;<>]+@[^@\s,;<>]+\.[^@\s,;<>]+", email):
            return sheets_tool_error(step_id, "Could not manage spreadsheet permission", HTTPException(status_code=422, detail="Enter one valid email address."))
        if action not in {"change", "remove"} or (action == "change" and role.strip().lower() not in role_map):
            return sheets_tool_error(step_id, "Could not manage spreadsheet permission", HTTPException(status_code=422, detail="Choose change/remove and a valid permission role."))
        emit_step(step_id, "running", "Preparing spreadsheet permission change", email)
        try:
            sheets_service, drive_service = sheets_clients()
            metadata = spreadsheet_metadata(sheets_service, spreadsheet_id)
            title = metadata.get("properties", {}).get("title", "Untitled spreadsheet")
            permissions = drive_service.permissions().list(
                fileId=spreadsheet_id,
                fields="permissions(id,type,emailAddress,role)",
                supportsAllDrives=True,
            ).execute().get("permissions", [])
            permission = next(
                (
                    item for item in permissions
                    if item.get("type") == "user"
                    and item.get("emailAddress", "").casefold() == email.casefold()
                ),
                None,
            )
            if not permission:
                raise HTTPException(status_code=404, detail=f"No named permission was found for {email}.")
        except (HttpError, HTTPException, ValueError) as error:
            return sheets_tool_error(step_id, "Could not find spreadsheet permission", error)

        next_role = role_map[role.strip().lower()] if action == "change" else ""
        preview = {
            "action": "Change permission" if action == "change" else "Remove permission",
            "spreadsheet_title": title,
            "email_addresses": [email],
            "current_role": permission.get("role", ""),
            "role": next_role,
        }
        decision = request_sheets_approval("sheets_permission", preview)
        if decision == "deny" or cancellation_event.is_set():
            emit_step(str(uuid.uuid4()), "warning", f"Cancelled permission change for {email}")
            return "The user cancelled the permission change. No access was changed."

        change_step_id = str(uuid.uuid4())
        emit_step(change_step_id, "running", f"{'Changing' if action == 'change' else 'Removing'} spreadsheet permission", email)
        try:
            ensure_turn_active()
            if action == "change":
                drive_service.permissions().update(
                    fileId=spreadsheet_id,
                    permissionId=permission["id"],
                    body={"role": next_role},
                    fields="id,emailAddress,role",
                    supportsAllDrives=True,
                ).execute()
            else:
                drive_service.permissions().delete(
                    fileId=spreadsheet_id,
                    permissionId=permission["id"],
                    supportsAllDrives=True,
                ).execute()
        except (HttpError, HTTPException) as error:
            return sheets_tool_error(change_step_id, "Could not change spreadsheet permission", error)
        emit_step(change_step_id, "completed", f"Spreadsheet permission {action} completed", email)
        return f"Spreadsheet permission {action} completed for {email}."

    def cancellable_tool(tool):
        @wraps(tool)
        def guarded_tool(*args, **kwargs):
            if cancellation_event.is_set():
                return "The user stopped this response. Do not perform any further actions."
            return tool(*args, **kwargs)

        return guarded_tool

    local_now = datetime.datetime.now().astimezone()
    today_str = local_now.strftime("%Y/%m/%d")
    local_timezone = local_now.strftime("%Z (UTC%z)")
    system_prompt = f"""
    You are D25, a fast, helpful personal AI assistant for {display_name} ({user_email}) modeled after GitHub Copilot. Today's local date is {today_str}; the user's local timezone is {local_timezone}.

    SCOPE & OUT-OF-SCOPE REQUESTS:
- Your connected actions are limited to Gmail, Google Calendar, and Google Sheets as described below. Do not imply you can access or take actions in other apps or services.
- If a request is unrelated to these capabilities, politely say it is outside what you can help with here, briefly mention the relevant Gmail, Calendar, or Sheets tasks you can do, and invite the user to choose one. Keep the redirect concise and natural; do not call tools for unrelated requests.
- Answer brief greetings and conversational acknowledgments naturally without showing the capability list. If an unrelated question has a useful connection to email, scheduling, or spreadsheets, offer that specific related help instead of a generic list.

    GMAIL CAPABILITIES & RULES:
- Use `get_emails` whenever the user asks to read, search, or summarize emails.
- STRICT INBOX FILTERING:
  - By default, ONLY fetch and summarize emails from the **Primary Inbox** (`category:primary`)—NEVER include Promotional, Social, Forums, or automated marketing emails unless explicitly requested.
  - For "today's emails", pass `query="category:primary after:{today_str}"`.
  - For "last 5 emails", pass `query="category:primary"` and `max_results=5`.
  - ONLY when the user specifies a particular sender or topic (e.g., "emails from Rahul"), search specifically for that sender (`query="from:rahul"`, `include_all_categories=True`).
- When summarizing emails inside the chat, present crisp, scannable bullet points with sender, core topic, and action items.

    EMAIL WRITING STYLE (WHEN SENDING OR REPLYING):
- When calling `send_email` or `reply_to_email`, write in **natural, flowing human paragraphs**—NOT structured AI templates or bullet lists with "**Objective:**", "**Key Focus Areas:**", or "**Action Items:**".
- NEVER use Markdown syntax (`**`, `##`, `- `) inside email bodies unless the user explicitly asks for a bulleted list.
- Keep emails polite, conversational, and concise—stick strictly to the user's points without adding filler unless asked to explain.
- Use natural contractions (e.g., "I'm", "we'll", "it's") and a natural sentence-case subject line.
- NEVER use AI clichés ("I hope this email finds you well", "pleased to confirm", "at your earliest convenience") and NEVER add "{display_name}" at the end of `body` (the HTML template adds it automatically).

    GOOGLE CALENDAR CAPABILITIES & RULES:
- Use `get_calendar_events` to view or search the user's Google Calendar. Search windows use ISO-8601 dates/times; when the user gives a relative date, calculate it from today's local date and timezone.
- Use `create_calendar_event`, `update_calendar_event`, and `delete_calendar_event` for event changes. For an update or cancellation, first call `get_calendar_events` and use the exact returned event ID; never guess an ID. If multiple events match, ask which one.
- Before creating, updating, or cancelling an event, show the user the approval card generated by the tool and wait for approval. Never claim a change succeeded unless the tool confirms it.
- Timed events must use ISO-8601 start/end timestamps in the user's local timezone. All-day events use `start_date` and inclusive `end_date`. Preserve existing event details the user did not ask to change.
- If a new timed event has a start but no duration, use one hour; if a new all-day event has no end date, use the same date for both. Ask a brief follow-up when the title, date, or start time is unclear.
- Google Calendar sends guest invitations, updates, or cancellations when the user approves the action.

    GOOGLE SHEETS CAPABILITIES & RULES:
- Use `find_spreadsheets` to locate a file by name, then use its exact returned ID. If several files match, ask which one. Use the Sheets tools for creating spreadsheets, reading ranges, editing/appending/clearing cells, managing worksheet tabs, professional formatting, formulas, sorting, filters, charts, and permissions.
- Spreadsheet values beginning with '=' are formulas. Use the user's requested formula; do not invent business logic. Ask before clearing cells or deleting a worksheet.
- For downloads, support XLSX, CSV, PDF, Word (DOCX), and PowerPoint (PPTX). If the format is not specified, ask the user to choose before calling `download_spreadsheet`. CSV/Word/PowerPoint export one worksheet; ask which tab when the spreadsheet has multiple tabs and the user has not selected one. XLSX/PDF export the whole spreadsheet.
- Share only with named email addresses; never create public/link-wide permissions. Default to the least-privileged Viewer role unless the user asks for commenter/editor access. List permissions without confirmation; require the approval card before granting, changing, or removing access, and before emailing a spreadsheet or attachment.
- Downloads, creates, and edits do not need an approval card. A download link expires after 30 minutes. If the user asks to email the file, use both a Google Sheets link and an attachment; ask for the attachment format if they did not specify one. If they only ask to grant access, send the Google Drive invitation without attaching a file.
- Do not claim a download, edit, share, or permission change succeeded unless its tool confirms it. Tell the user clearly if an operation partially succeeded.
    """
    mode_instructions = {
        "ask": """
CURRENT MODE: ASK
- Answer questions and retrieve information when useful.
- This mode is read-only. Do not send, reply to, modify, share, create, or delete anything.
- If the user asks for an action, explain that they can switch to Act mode.
""",
        "plan": """
CURRENT MODE: PLAN
- Do not perform any changes. You may use read-only tools to ground the plan in the user's data.
- Break the request into actionable steps and identify the Google services required.
- If key information is missing, ask concise clarifying questions and do not mark the plan ready.
- When a complete plan is ready for user approval, end with the exact marker [[D25_PLAN_READY]] on its own final line.
- Do not include that marker when asking questions or when no actionable plan is proposed.
""",
        "act": """
CURRENT MODE: ACT
- Carry out the user's requested or explicitly approved workflow using the available tools.
- When continuing a plan, follow the approved steps exactly and do not add unrelated actions.
- Existing action-specific approval cards are still required; plan approval does not replace them.
- Report results only when confirmed by the corresponding tool.
""",
    }[mode]
    system_prompt += "\n\n" + mode_instructions

    thinking_step_id = str(uuid.uuid4())
    emit_step(thinking_step_id, "running", "Thinking...")

    history = load_gemini_history_from_db(session_id, user_email)
    last_error = None

    all_tools = [
        get_emails,
        send_email,
        reply_to_email,
        modify_emails,
        get_calendar_events,
        create_calendar_event,
        update_calendar_event,
        delete_calendar_event,
        find_spreadsheets,
        get_spreadsheet_info,
        read_spreadsheet_range,
        create_spreadsheet,
        write_sheet_range,
        manage_sheet_tabs,
        format_spreadsheet,
        sort_sheet_range,
        set_sheet_filter,
        create_sheet_chart,
        download_spreadsheet,
        share_spreadsheet,
        list_spreadsheet_permissions,
        manage_spreadsheet_permission,
    ]
    read_only_tools = [
        get_emails,
        get_calendar_events,
        find_spreadsheets,
        get_spreadsheet_info,
        read_spreadsheet_range,
        list_spreadsheet_permissions,
    ]
    available_tools = all_tools if mode == "act" else read_only_tools

    try:
        for model_name in FALLBACK_MODELS:
            try:
                if cancellation_event.is_set():
                    emit_step(thinking_step_id, "completed", "Stopped")
                    return
                chat = AI_CLIENT.chats.create(
                    model=model_name,
                    history=history,
                    config=types.GenerateContentConfig(
                        system_instruction=system_prompt,
                        tools=[cancellable_tool(tool) for tool in available_tools],
                    ),
                )
                reply_text = ""
                response_stream = chat.send_message_stream(user_message)
                for chunk in response_stream:
                    if cancellation_event.is_set():
                        response_stream.close()
                        emit_step(thinking_step_id, "completed", "Stopped")
                        return
                    chunk_text = chunk.text or ""
                    if chunk_text:
                        reply_text += chunk_text
                        loop.call_soon_threadsafe(
                            event_queue.put_nowait,
                            format_sse("message_delta", {"text": chunk_text}),
                        )
                if cancellation_event.is_set():
                    emit_step(thinking_step_id, "completed", "Stopped")
                    return
                duration_sec = max(1, int(time.time() - start_ts))
                plan_ready = mode == "plan" and "[[D25_PLAN_READY]]" in reply_text
                if mode == "plan":
                    reply_text = reply_text.replace("[[D25_PLAN_READY]]", "").rstrip()
                turn_record["plan_ready"] = plan_ready

                emit_step(thinking_step_id, "completed", "Done")
                save_turn_to_mongodb(session_id, user_email, user_message, reply_text, turn_record, duration_sec)
                if cancellation_event.is_set():
                    emit_step(thinking_step_id, "completed", "Stopped")
                    return

                loop.call_soon_threadsafe(
                    event_queue.put_nowait,
                    format_sse("message", {"text": reply_text, "plan_ready": plan_ready}),
                )
                return
            except Exception as e:
                last_error = e
                if cancellation_event.is_set():
                    emit_step(thinking_step_id, "completed", "Stopped")
                    return
                if "503" in str(e) or "UNAVAILABLE" in str(e):
                    loop.call_soon_threadsafe(
                        event_queue.put_nowait,
                        format_sse("message_reset", {}),
                    )
                    emit_step(thinking_step_id, "running", f"Model {model_name} busy, switching...")
                    time.sleep(1)
                    continue
                raise e
        raise last_error
    except Exception as e:
        emit_step(thinking_step_id, "warning", "Encountered an error", str(e))
        loop.call_soon_threadsafe(
            event_queue.put_nowait,
            format_sse("error", {"message": str(e)}),
        )
    finally:
        loop.call_soon_threadsafe(event_queue.put_nowait, format_sse("done", {}))


def run_registered_chat_turn(
    turn_id: str,
    session_id: str,
    user_email: str,
    gmail_service,
    user_message: str,
    mode: str,
    event_queue: asyncio.Queue,
    loop: asyncio.AbstractEventLoop,
    cancellation_event: threading.Event,
) -> None:
    try:
        run_agent_turn(
            session_id,
            user_email,
            gmail_service,
            user_message,
            mode,
            event_queue,
            loop,
            cancellation_event,
        )
    except Exception as error:
        LOGGER.exception("Chat turn failed before completion.")
        loop.call_soon_threadsafe(
            event_queue.put_nowait,
            format_sse("error", {"message": str(error)}),
        )
        loop.call_soon_threadsafe(event_queue.put_nowait, format_sse("done", {}))
    finally:
        with ACTIVE_CHAT_TURNS_LOCK:
            record = ACTIVE_CHAT_TURNS.get(turn_id)
            if record and record["cancellation_event"] is cancellation_event:
                ACTIVE_CHAT_TURNS.pop(turn_id, None)


# ---------------------------------------------------------------------------
# Google Auth Routes (Sign In / Switch Account / Sign Out)
# ---------------------------------------------------------------------------
@app.get("/api/auth/status")
async def auth_status():
    email, creds = get_credentials_for_active_user()
    return {
        "authenticated": bool(email and creds),
        "user_email": email,
    }


@app.get("/api/auth/login")
async def auth_login():
    """Redirects to Google's account chooser so the user can sign in with any Google account."""
    flow = Flow.from_client_secrets_file(
        str(BASE_DIR / CREDENTIALS_FILE),
        scopes=SCOPES,
        redirect_uri=REDIRECT_URI,
    )
    auth_url, state = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="select_account consent",
    )
    OAUTH_STATES[state] = flow
    return RedirectResponse(url=auth_url)


@app.get("/api/auth/callback")
async def auth_callback(request: FastAPIRequest):
    state = request.query_params.get("state")
    code = request.query_params.get("code")
    if not state or state not in OAUTH_STATES:
        raise HTTPException(status_code=400, detail="Invalid or expired OAuth state. Please try signing in again.")

    flow = OAUTH_STATES.pop(state)
    flow.fetch_token(code=code)
    creds = flow.credentials

    gmail_svc = build("gmail", "v1", credentials=creds)
    profile = gmail_svc.users().getProfile(userId="me").execute()
    user_email = profile.get("emailAddress", "").lower().strip()

    _token_path_for(user_email).write_text(creds.to_json(), encoding="utf-8")
    set_active_user_email(user_email)
    return RedirectResponse(url="/")


@app.post("/api/auth/logout")
async def auth_logout(forget_token: bool = True):
    """Logs out the active Google account and removes its local token."""
    current = get_active_user_email()
    if current and forget_token:
        tpath = _token_path_for(current)
        if tpath.exists():
            tpath.unlink()
    legacy_path = BASE_DIR / LEGACY_TOKEN_FILE
    if forget_token and legacy_path.exists():
        legacy_path.unlink()

    set_active_user_email(None)
    return {"status": "logged_out"}


# ---------------------------------------------------------------------------
# FastAPI Routes (Frontend + Chat + MongoDB History & Permissions)
# ---------------------------------------------------------------------------
@app.get("/")
async def serve_frontend():
    return FileResponse(BASE_DIR / "index.html")


@app.get("/favicon.ico", include_in_schema=False)
async def serve_favicon():
    return FileResponse(
        BASE_DIR / "static" / "favicon.ico",
        media_type="image/vnd.microsoft.icon",
    )


@app.get("/api/me")
async def get_active_user():
    email, _ = require_gmail_service()
    return {"user_email": email, "db_connected": True}


@app.get("/api/sheets/download/{download_id}")
async def download_sheet_export(download_id: str):
    user_email, credentials = get_credentials_for_active_user()
    if not user_email or not credentials:
        raise HTTPException(status_code=401, detail="Sign in with Google to download this export.")

    with SHEETS_DOWNLOAD_LOCK:
        record = SHEETS_DOWNLOADS.get(download_id)
        if record and record["expires_at"] <= time.time():
            SHEETS_DOWNLOADS.pop(download_id, None)
            record = None
    if not record:
        raise HTTPException(status_code=404, detail="This spreadsheet download has expired.")
    if record["user_email"] != user_email.lower():
        raise HTTPException(status_code=404, detail="This spreadsheet download was not found.")

    return Response(
        content=record["content"],
        media_type=record["content_type"],
        headers={
            "Content-Disposition": f'attachment; filename="{record["filename"]}"',
            "Cache-Control": "no-store",
        },
    )


@app.post("/api/chat")
async def chat_stream(req: ChatRequest):
    user_email, gmail_service = require_gmail_service()
    session_id = req.session_id or str(uuid.uuid4())
    turn_id = req.turn_id or str(uuid.uuid4())
    cancellation_event = threading.Event()
    event_queue: asyncio.Queue = asyncio.Queue()
    loop = asyncio.get_running_loop()

    with ACTIVE_CHAT_TURNS_LOCK:
        if turn_id in ACTIVE_CHAT_TURNS:
            raise HTTPException(status_code=409, detail="This chat turn is already running.")
        ACTIVE_CHAT_TURNS[turn_id] = {
            "user_email": user_email.lower(),
            "session_id": session_id,
            "cancellation_event": cancellation_event,
        }

    await event_queue.put(format_sse("session", {"session_id": session_id}))
    loop.run_in_executor(
        None,
        run_registered_chat_turn,
        turn_id,
        session_id,
        user_email,
        gmail_service,
        req.message,
        req.mode,
        event_queue,
        loop,
        cancellation_event,
    )

    async def event_generator():
        while True:
            chunk = await event_queue.get()
            yield chunk
            if '"type": "done"' in chunk:
                break

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@app.post("/api/chat/stop")
async def stop_chat_turn(req: ChatStopRequest):
    user_email, _ = require_gmail_service()
    with ACTIVE_CHAT_TURNS_LOCK:
        record = ACTIVE_CHAT_TURNS.get(req.turn_id)
        if not record or record["user_email"] != user_email.lower():
            raise HTTPException(status_code=404, detail="This chat response is no longer running.")
        record["cancellation_event"].set()
    return {"stop_requested": True}


@app.post("/api/approve")
async def resolve_approval(body: ApprovalDecision):
    if body.approval_id not in PENDING_APPROVALS:
        raise HTTPException(status_code=404, detail="Approval request expired or not found.")

    if body.decision not in {"allow_once", "allow_always", "deny"}:
        raise HTTPException(status_code=400, detail="Invalid decision value.")

    record = PENDING_APPROVALS[body.approval_id]
    if body.edits and body.decision != "deny":
        action_type = record.get("action_type")
        preview = record.get("preview")
        if action_type not in {"send_email", "reply_to_email"} or not isinstance(preview, dict):
            raise HTTPException(status_code=400, detail="This approval does not support email edits.")

        if body.edits.to is not None:
            if action_type != "send_email":
                raise HTTPException(status_code=400, detail="The recipient cannot be changed for a reply.")
            recipient = body.edits.to.strip()
            address = parseaddr(recipient)[1]
            if (
                not recipient
                or "\r" in recipient
                or "\n" in recipient
                or "," in recipient
                or ";" in recipient
                or not address
                or address.count("@") != 1
                or any(char.isspace() for char in address)
            ):
                raise HTTPException(status_code=400, detail="Enter one valid recipient email address.")
            preview["to"] = recipient
            record["recipient"] = address.lower()

        if body.edits.subject is not None:
            subject = body.edits.subject.strip()
            if not subject or "\r" in subject or "\n" in subject:
                raise HTTPException(status_code=400, detail="Email subject cannot be empty or contain new lines.")
            preview["subject"] = subject

        if body.edits.body is not None:
            preview["body"] = body.edits.body.strip()

        if body.edits.signature is not None:
            preview["signature"] = body.edits.signature.strip()

    record["decision"] = body.decision
    record["event"].set()
    return {"status": "ok", "decision": body.decision}


@app.get("/api/sessions")
async def list_user_sessions():
    """Returns all saved sessions for the active user from MongoDB."""
    user_email, _ = require_gmail_service()
    cursor = SESSIONS_COL.find(
        {"user_email": user_email},
        {"_id": 0, "session_id": 1, "title": 1, "updated_at": 1},
    ).sort("updated_at", DESCENDING).limit(30)
    return {"sessions": list(cursor)}


@app.get("/api/sessions/{session_id}")
async def get_session_messages(session_id: str):
    """Loads every turn (prompts, steps, approvals, and replies) for a past session."""
    user_email, _ = require_gmail_service()
    session = SESSIONS_COL.find_one({"session_id": session_id, "user_email": user_email}, {"_id": 0, "gemini_history": 0})
    if not session:
        raise HTTPException(status_code=404, detail="Session not found.")

    msgs = list(
        MESSAGES_COL.find({"session_id": session_id, "user_email": user_email}, {"_id": 0}).sort("created_at", ASCENDING)
    )
    return {"session": session, "messages": msgs}


@app.delete("/api/sessions/{session_id}")
async def delete_session(session_id: str):
    user_email, _ = require_gmail_service()
    SESSIONS_COL.delete_one({"session_id": session_id, "user_email": user_email})
    MESSAGES_COL.delete_many({"session_id": session_id, "user_email": user_email})
    return {"status": "deleted"}


@app.get("/api/permissions")
async def get_permissions():
    user_email, _ = require_gmail_service()
    return load_permissions(user_email)


@app.delete("/api/permissions/{recipient_email}")
async def revoke_permission(recipient_email: str):
    user_email, _ = require_gmail_service()
    target = recipient_email.strip().lower()
    PERMISSIONS_COL.update_one(
        {"user_email": user_email},
        {"$pull": {"allowed_recipients": target}},
    )
    return load_permissions(user_email)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("backend:app", host="127.0.0.1", port=8000, reload=True)