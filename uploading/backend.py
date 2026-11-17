import os
import json
import time
import html
import uuid
import base64
import asyncio
import datetime
import re
from typing import Optional
from email.message import EmailMessage


from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, FileResponse
from pydantic import BaseModel
from fastapi.staticfiles import StaticFiles
from pathlib import Path

from google import genai
from google.genai import types
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

load_dotenv()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
CREDENTIALS_FILE = os.getenv("GOOGLE_CREDENTIALS_FILE", "credentials.json")
TOKEN_FILE = os.getenv("GOOGLE_TOKEN_FILE", "token.json")
PERMISSIONS_FILE = "permissions.json"

if not GEMINI_API_KEY:
    raise ValueError("Missing GEMINI_API_KEY in .env file.")

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

app = FastAPI(title="D25 Assistant Backend")

BASE_DIR = Path(__file__).resolve().parent
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Google Auth & Service Initialization
# ---------------------------------------------------------------------------
def get_google_credentials() -> Credentials:
    creds = None
    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(TOKEN_FILE, "w") as token:
            token.write(creds.to_json())

    return creds


CREDS = get_google_credentials()
GMAIL_SERVICE = build("gmail", "v1", credentials=CREDS)
AI_CLIENT = genai.Client(api_key=GEMINI_API_KEY)


# ---------------------------------------------------------------------------
# Persistent "Allow Always" Permission Store (Per Recipient)
# ---------------------------------------------------------------------------
def load_permissions() -> dict:
    if os.path.exists(PERMISSIONS_FILE):
        try:
            with open(PERMISSIONS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"allowed_recipients": []}


def save_permissions(data: dict) -> None:
    with open(PERMISSIONS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def is_recipient_trusted(email_addr: str) -> bool:
    clean_addr = _extract_raw_email(email_addr)
    perms = load_permissions()
    return clean_addr in [e.lower() for e in perms.get("allowed_recipients", [])]


def trust_recipient_always(email_addr: str) -> None:
    clean_addr = _extract_raw_email(email_addr)
    perms = load_permissions()
    allowed = [e.lower() for e in perms.get("allowed_recipients", [])]
    if clean_addr and clean_addr not in allowed:
        allowed.append(clean_addr)
        perms["allowed_recipients"] = allowed
        save_permissions(perms)


def _extract_raw_email(header_val: str) -> str:
    """Extracts 'user@domain.com' from 'Name <user@domain.com>' or returns lowercased string."""
    val = header_val.strip().lower()
    if "<" in val and ">" in val:
        return val.split("<")[-1].split(">")[0].strip()
    return val


# ---------------------------------------------------------------------------
# Session & Approval State Management
# ---------------------------------------------------------------------------
SESSIONS: dict[str, list] = {}
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
    """Converts basic markdown bold/bullets into clean editorial HTML so raw ** or - never leak into emails."""
    escaped = html.escape(text)
    # Convert **bold** to <strong>bold</strong>
    escaped = re.sub(r"\*\*(.+?)\*\*", r'<strong style="color: #111827; font-weight: 600;">\1</strong>', escaped)
    return escaped

def build_html_email(clean_body: str) -> str:
    """Clean, native-looking editorial HTML that reads like a genuine 1-to-1 human email."""
    blocks = [b.strip() for b in clean_body.split("\n\n") if b.strip()]
    html_blocks = []

    for block in blocks:
        lines = [line.strip() for line in block.split("\n") if line.strip()]
        # Check if this block is a bullet list
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
              <p style="margin: 28px 0 0 0; font-size: 15px; font-weight: 600; color: #111827;">Dhiraj Kumar</p>
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
# SSE Event Helper & Approval Gate
# ---------------------------------------------------------------------------
def format_sse(event_type: str, payload: dict) -> str:
    return f"data: {json.dumps({'type': event_type, **payload})}\n\n"


def request_user_approval_sync(
    event_queue: asyncio.Queue,
    loop: asyncio.AbstractEventLoop,
    action_type: str,
    recipient: str,
    preview_data: dict,
) -> str:
    """Pauses tool execution and streams an approval card to the frontend UI unless recipient is trusted."""
    if recipient and is_recipient_trusted(recipient):
        loop.call_soon_threadsafe(
            event_queue.put_nowait,
            format_sse(
                "step",
                {
                    "id": str(uuid.uuid4()),
                    "status": "completed",
                    "label": f"Auto-approved for trusted recipient ({_extract_raw_email(recipient)})",
                },
            ),
        )
        return "allow_always"

    approval_id = str(uuid.uuid4())
    decision_event = asyncio.Event()
    PENDING_APPROVALS[approval_id] = {
        "event": decision_event,
        "decision": "deny",
        "recipient": recipient,
    }

    # Emit step + approval prompt to the frontend
    loop.call_soon_threadsafe(
        event_queue.put_nowait,
        format_sse(
            "approval_required",
            {
                "approval_id": approval_id,
                "action_type": action_type,
                "recipient": _extract_raw_email(recipient) if recipient else "",
                "preview": preview_data,
            },
        ),
    )

    # Block this worker thread until the user clicks Allow Once / Allow Always / Deny in the UI
    future = asyncio.run_coroutine_threadsafe(decision_event.wait(), loop)
    future.result(timeout=300)  # 5-minute timeout

    record = PENDING_APPROVALS.pop(approval_id, {})
    decision = record.get("decision", "deny")

    if decision == "allow_always" and recipient:
        trust_recipient_always(recipient)

    return decision


# ---------------------------------------------------------------------------
# Agent Loop with Live Copilot-Style Step Emission
# ---------------------------------------------------------------------------
def run_agent_turn(
    session_id: str,
    user_message: str,
    event_queue: asyncio.Queue,
    loop: asyncio.AbstractEventLoop,
):
    def emit_step(step_id: str, status: str, label: str, detail: str = ""):
        loop.call_soon_threadsafe(
            event_queue.put_nowait,
            format_sse(
                "step",
                {
                    "id": step_id,
                    "status": status,  # "running" | "completed" | "warning"
                    "label": label,
                    "detail": detail,
                },
            ),
        )

    # --- Tool 1: Read / Search Primary Inbox (No approval needed) ---
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

        response = GMAIL_SERVICE.users().messages().list(userId="me", q=clean_query, maxResults=max_results).execute()
        messages = response.get("messages", [])

        if not messages:
            emit_step(step_id, "completed", "Checked Primary Inbox (0 emails matched)", clean_query)
            return [{"info": f"No Primary Inbox emails found matching query: '{clean_query}'"}]

        results = []
        for m in messages:
            detail = GMAIL_SERVICE.users().messages().get(userId="me", id=m["id"], format="full").execute()
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

    # --- Tool 2: Send New Email (Requires Permission) ---
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
            action_type="send_email",
            recipient=to_email,
            preview_data={
                "to": to_email,
                "subject": clean_subject,
                "body": clean_body,
                "signature": "Dhiraj Kumar",
                "html_preview": build_html_email(clean_body),
            },
        )

        if decision == "deny":
            cancel_step_id = str(uuid.uuid4())
            emit_step(cancel_step_id, "warning", f"Cancelled sending email to {to_email}")
            return "User denied permission to send this email."

        send_step_id = str(uuid.uuid4())
        emit_step(send_step_id, "running", f"Sending email to {to_email}...")

        message = EmailMessage()
        message["To"] = to_email
        message["Subject"] = clean_subject
        message.set_content(f"{clean_body}\n\nDhiraj Kumar")
        message.add_alternative(build_html_email(clean_body), subtype="html")

        encoded_message = base64.urlsafe_b64encode(message.as_bytes()).decode()
        GMAIL_SERVICE.users().messages().send(userId="me", body={"raw": encoded_message}).execute()

        emit_step(send_step_id, "completed", f"Sent email to {to_email}", f"Subject: {clean_subject}")
        return f"Email successfully sent to {to_email} with subject '{clean_subject}'."

    # --- Tool 3: Reply Inside Thread (Requires Permission) ---
    def reply_to_email(message_id: str, body: str) -> str:
        """Replies directly inside an existing Gmail thread after permission check.

        Args:
            message_id: The Gmail message 'id' returned by `get_emails` that you are replying to.
            body: Concise, polite reply text (do NOT include sign-off).
        """
        draft_step_id = str(uuid.uuid4())
        orig = GMAIL_SERVICE.users().messages().get(userId="me", id=message_id, format="metadata").execute()
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
            action_type="reply_to_email",
            recipient=orig_from,
            preview_data={
                "to": orig_from,
                "subject": reply_subject,
                "body": clean_body,
                "signature": "Dhiraj Kumar",
                "html_preview": build_html_email(clean_body),
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

        message.set_content(f"{clean_body}\n\nDhiraj Kumar")
        message.add_alternative(build_html_email(clean_body), subtype="html")

        encoded_message = base64.urlsafe_b64encode(message.as_bytes()).decode()
        GMAIL_SERVICE.users().messages().send(
            userId="me", body={"raw": encoded_message, "threadId": thread_id}
        ).execute()

        emit_step(send_step_id, "completed", f"Sent reply to {orig_from}")
        return f"Reply sent to {orig_from} on thread '{reply_subject}'."

    # --- Tool 4: Modify / Trash / Mark Read (Requires Permission) ---
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
            action_type="modify_emails",
            recipient="",  # Always ask before modifying/deleting emails
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
            GMAIL_SERVICE.users().messages().batchModify(
                userId="me", body={"ids": message_ids, "removeLabelIds": ["UNREAD"]}
            ).execute()
        elif action == "archive":
            GMAIL_SERVICE.users().messages().batchModify(
                userId="me", body={"ids": message_ids, "removeLabelIds": ["INBOX"]}
            ).execute()
        elif action == "trash":
            for mid in message_ids:
                GMAIL_SERVICE.users().messages().trash(userId="me", id=mid).execute()
        else:
            return f"Unsupported action: {action}"

        emit_step(mod_step_id, "completed", f"Completed '{action}' on {len(message_ids)} email(s)")
        return f"Successfully performed '{action}' on {len(message_ids)} email(s)."

    # --- Execute Gemini Agent Loop ---
    today_str = datetime.date.today().strftime("%Y/%m/%d")
    system_prompt = f"""
    You are D25, a fast, helpful personal AI assistant for Dhiraj Kumar modeled after GitHub Copilot. Today's date is {today_str}.

    GMAIL CAPABILITIES & RULES:
    - Use `get_emails` whenever the user asks to read, search, or summarize emails.
    - STRICT INBOX FILTERING:
      * By default, ONLY fetch and summarize emails from the **Primary Inbox** (`category:primary`)—NEVER include Promotional, Social, Forums, or automated marketing emails unless explicitly requested.
      * For "today's emails", pass `query="category:primary after:{today_str}"`.
      * For "last 5 emails", pass `query="category:primary"` and `max_results=5`.
      * ONLY when the user specifies a particular sender or topic (e.g., "emails from Rahul"), search specifically for that sender (`query="from:rahul"`, `include_all_categories=True`).
    - When summarizing emails inside the chat, present crisp, scannable bullet points with sender, core topic, and action items.

    EMAIL WRITING STYLE (WHEN SENDING OR REPLYING):
    - When calling `send_email` or `reply_to_email`, write in **natural, flowing human paragraphs**—NOT structured AI templates or bullet lists with "**Objective:**", "**Key Focus Areas:**", or "**Action Items:**".
    - NEVER use Markdown syntax (`**`, `##`, `- `) inside email bodies unless the user explicitly asks for a bulleted list.
    - Keep emails polite, conversational, and concise—stick strictly to the user's points without adding filler unless asked to explain.
    - Use natural contractions (e.g., "I'm", "we'll", "it's") and a natural sentence-case subject line.
    - NEVER use AI clichés ("I hope this email finds you well", "pleased to confirm", "at your earliest convenience") and NEVER add "Dhiraj Kumar" at the end of `body` (the HTML template adds it automatically).
    """

    thinking_step_id = str(uuid.uuid4())
    emit_step(thinking_step_id, "running", "Thinking...")

    history = SESSIONS.get(session_id, [])
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
                SESSIONS[session_id] = chat.get_history()

                emit_step(thinking_step_id, "completed", "Done")
                loop.call_soon_threadsafe(
                    event_queue.put_nowait,
                    format_sse("message", {"text": reply.text or ""}),
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
# FastAPI HTTP Endpoints
# ---------------------------------------------------------------------------
@app.get("/")
async def serve_frontend():
    return FileResponse("index.html")

@app.post("/api/chat")
async def chat_stream(req: ChatRequest):
    session_id = req.session_id or str(uuid.uuid4())
    event_queue: asyncio.Queue = asyncio.Queue()
    loop = asyncio.get_running_loop()

    # Emit session_id first so frontend tracks multi-turn history
    await event_queue.put(format_sse("session", {"session_id": session_id}))

    # Run blocking Google API + Gemini tool loop in a background thread
    loop.run_in_executor(None, run_agent_turn, session_id, req.message, event_queue, loop)

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


@app.get("/api/permissions")
async def get_permissions():
    """Returns the list of recipients marked 'Allow Always' so the UI can view or revoke them."""
    return load_permissions()


@app.delete("/api/permissions/{recipient_email}")
async def revoke_permission(recipient_email: str):
    perms = load_permissions()
    target = recipient_email.strip().lower()
    perms["allowed_recipients"] = [e for e in perms.get("allowed_recipients", []) if e.lower() != target]
    save_permissions(perms)
    return {"status": "revoked", "allowed_recipients": perms["allowed_recipients"]}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("backend:app", host="127.0.0.1", port=8000, reload=True)