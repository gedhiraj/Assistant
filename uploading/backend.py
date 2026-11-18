import os
import re
import json
import time
import html
import uuid
import base64
import asyncio
import datetime
import certifi
from typing import Optional
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid, parseaddr
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request as FastAPIRequest
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, FileResponse, RedirectResponse
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


class ChatRequest(BaseModel):
    session_id: Optional[str] = None
    message: str


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
) -> str:
    """Pauses execution for approval unless recipient is trusted, and logs the approval record to MongoDB."""
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
    future.result(timeout=300)

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
        "steps": turn_record["steps"],
        "approvals": turn_record["approvals"],
        "duration_sec": duration_sec,
        "created_at": now_iso,
    })


# ---------------------------------------------------------------------------
# Agent Loop with Live Copilot-Style Step Emission + MongoDB Logging
# ---------------------------------------------------------------------------
def run_agent_turn(
    session_id: str,
    user_email: str,
    gmail_service,
    user_message: str,
    event_queue: asyncio.Queue,
    loop: asyncio.AbstractEventLoop,
):
    start_ts = time.time()
    turn_record = {"steps": [], "approvals": []}
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
        )

        if decision == "deny":
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
        )

        if decision == "deny":
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
        )

        if decision == "deny":
            emit_step(str(uuid.uuid4()), "warning", f"Cancelled '{action}' on {len(message_ids)} email(s)")
            return f"User denied permission to {action} emails."

        mod_step_id = str(uuid.uuid4())
        emit_step(mod_step_id, "running", f"Applying '{action}' to {len(message_ids)} email(s)...")

        if action == "mark_read":
            gmail_service.users().messages().batchModify(
                userId="me", body={"ids": message_ids, "removeLabelIds": ["UNREAD"]}
            ).execute()
        elif action == "archive":
            gmail_service.users().messages().batchModify(
                userId="me", body={"ids": message_ids, "removeLabelIds": ["INBOX"]}
            ).execute()
        elif action == "trash":
            for mid in message_ids:
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
        if decision == "deny":
            emit_step(str(uuid.uuid4()), "warning", f"Cancelled Calendar event '{payload.summary.strip()}'")
            return "The user cancelled the Calendar event. It was not created."

        create_step_id = str(uuid.uuid4())
        emit_step(create_step_id, "running", "Creating Google Calendar event", payload.summary.strip())
        try:
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
        if decision == "deny":
            emit_step(str(uuid.uuid4()), "warning", f"Cancelled Calendar update for '{current['summary']}'")
            return "The user cancelled the Calendar update. No changes were made."

        update_step_id = str(uuid.uuid4())
        emit_step(update_step_id, "running", "Updating Google Calendar event", current["summary"])
        try:
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
        if decision == "deny":
            emit_step(str(uuid.uuid4()), "warning", f"Cancelled request to delete '{current['summary']}'")
            return "The user cancelled the Calendar cancellation. The event remains scheduled."

        delete_step_id = str(uuid.uuid4())
        emit_step(delete_step_id, "running", "Cancelling Google Calendar event", current["summary"])
        try:
            service.events().delete(
                calendarId="primary",
                eventId=event_id,
                sendUpdates="all",
            ).execute()
        except (HttpError, HTTPException) as error:
            return calendar_tool_error(delete_step_id, "Could not cancel Calendar event", error)
        emit_step(delete_step_id, "completed", f"Cancelled Calendar event: {current['summary']}")
        return f"Cancelled Calendar event '{current['summary']}' and notified its guests."

    local_now = datetime.datetime.now().astimezone()
    today_str = local_now.strftime("%Y/%m/%d")
    local_timezone = local_now.strftime("%Z (UTC%z)")
    system_prompt = f"""
    You are D25, a fast, helpful personal AI assistant for {display_name} ({user_email}) modeled after GitHub Copilot. Today's local date is {today_str}; the user's local timezone is {local_timezone}.

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
    """

    thinking_step_id = str(uuid.uuid4())
    emit_step(thinking_step_id, "running", "Thinking...")

    history = load_gemini_history_from_db(session_id, user_email)
    last_error = None

    try:
        for model_name in FALLBACK_MODELS:
            try:
                chat = AI_CLIENT.chats.create(
                    model=model_name,
                    history=history,
                    config=types.GenerateContentConfig(
                        system_instruction=system_prompt,
                        tools=[
                            get_emails,
                            send_email,
                            reply_to_email,
                            modify_emails,
                            get_calendar_events,
                            create_calendar_event,
                            update_calendar_event,
                            delete_calendar_event,
                        ],
                    ),
                )
                reply = chat.send_message(user_message)
                reply_text = reply.text or ""
                duration_sec = max(1, int(time.time() - start_ts))

                emit_step(thinking_step_id, "completed", "Done")
                save_turn_to_mongodb(session_id, user_email, user_message, reply_text, turn_record, duration_sec)

                loop.call_soon_threadsafe(
                    event_queue.put_nowait,
                    format_sse("message", {"text": reply_text}),
                )
                return
            except Exception as e:
                last_error = e
                if "503" in str(e) or "UNAVAILABLE" in str(e):
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


@app.post("/api/chat")
async def chat_stream(req: ChatRequest):
    user_email, gmail_service = require_gmail_service()
    session_id = req.session_id or str(uuid.uuid4())
    event_queue: asyncio.Queue = asyncio.Queue()
    loop = asyncio.get_running_loop()

    await event_queue.put(format_sse("session", {"session_id": session_id}))
    loop.run_in_executor(None, run_agent_turn, session_id, user_email, gmail_service, req.message, event_queue, loop)

    async def event_generator():
        while True:
            chunk = await event_queue.get()
            yield chunk
            if '"type": "done"' in chunk:
                break

    return StreamingResponse(event_generator(), media_type="text/event-stream")


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