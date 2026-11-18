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


class ApprovalDecision(BaseModel):
    approval_id: str
    decision: str  # "allow_once" | "allow_always" | "deny"


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
              <p style="margin: 28px 0 0 0; font-size: 15px; font-weight: 600; color: #111827;">{html.escape(sender_name)}</p>
            </td>
          </tr>
        </table>
      </td>
    </tr>
  </table>
</body>
</html>"""


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

    def emit_step(step_id: str, status: str, label: str, detail: str = ""):
        step_obj = {"id": step_id, "status": status, "label": label, "detail": detail}
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

        decision = request_user_approval_sync(
            event_queue=event_queue,
            loop=loop,
            user_email=user_email,
            turn_record=turn_record,
            action_type="send_email",
            recipient=to_email,
            preview_data={
                "to": to_email,
                "subject": clean_subject,
                "body": clean_body,
                "signature": display_name,
            },
        )

        if decision == "deny":
            emit_step(str(uuid.uuid4()), "warning", f"Cancelled sending email to {to_email}")
            return "User denied permission to send this email."

        send_step_id = str(uuid.uuid4())
        emit_step(send_step_id, "running", f"Sending email to {to_email}...")

        message = EmailMessage()
        message["To"] = to_email
        message["Subject"] = clean_subject
        message.set_content(f"{clean_body.replace('**', '')}\n\n{display_name}")
        message.add_alternative(build_html_email(clean_body, display_name), subtype="html")

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

        decision = request_user_approval_sync(
            event_queue=event_queue,
            loop=loop,
            user_email=user_email,
            turn_record=turn_record,
            action_type="reply_to_email",
            recipient=orig_from,
            preview_data={
                "to": orig_from,
                "subject": reply_subject,
                "body": clean_body,
                "signature": display_name,
            },
        )

        if decision == "deny":
            emit_step(str(uuid.uuid4()), "warning", f"Cancelled reply to {orig_from}")
            return "User denied permission to send this reply."

        send_step_id = str(uuid.uuid4())
        emit_step(send_step_id, "running", f"Sending reply to {orig_from}...")

        message = EmailMessage()
        message["To"] = orig_from
        message["Subject"] = reply_subject
        if orig_msg_id:
            message["In-Reply-To"] = orig_msg_id
            message["References"] = orig_msg_id

        message.set_content(f"{clean_body.replace('**', '')}\n\n{display_name}")
        message.add_alternative(build_html_email(clean_body, display_name), subtype="html")

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

    today_str = datetime.date.today().strftime("%Y/%m/%d")
    system_prompt = f"""
    You are D25, a fast, helpful personal AI assistant for {display_name} ({user_email}) modeled after GitHub Copilot. Today's date is {today_str}.

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
                        tools=[get_emails, send_email, reply_to_email, modify_emails],
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