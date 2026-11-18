Because you already enabled the **`gmail.modify`** scope in `D25`, your assistant isn't limited to just sending emails—it can read full email bodies, search your inbox using Gmail filters, summarize today's messages, draft replies inside existing threads, mark emails as read, and organize labels.

### What You Can Ask D25 Once You Add Gmail Reader & Manager Tools

Right now, `assistant.py` only fetches the **subjects** of your last 5 emails at startup and has one tool (`send_email`). By adding a few Gmail tools to `assistant.py`, you can ask D25 things like:

* **Summarize today's or recent emails:** *"Summarize all emails I received today and list the key action items."* or *"Read and summarize my last 5 emails."*
* **Search specific senders, topics, or attachments:** *"Find all emails from Oracle or GitHub this week and summarize them."* or *"Check if I got any interview emails today."*
* **Read full email details:** *"Open the latest email from Rahul and give me the exact meeting link and time."*
* **Reply directly inside an email thread:** *"Reply to the latest email from gedhiraj@gmail.com saying I received it and will share the update by tomorrow."* (keeps the `Re:` subject and thread history intact).
* **Create drafts without sending:** *"Save a draft email to my manager about Friday's leave so I can review it in Gmail later."*
* **Clean up your inbox:** *"Mark all promotional or unread emails from yesterday as read"* or *"Archive the last 3 emails from LinkedIn."*

---

### Upgraded `assistant.py` (Full Gmail Read, Search, Summarize, Reply & Cleanup Tools)

Replace your `assistant.py` with this upgraded version—it keeps your human-style HTML email template and adds **`get_emails`**, **`reply_to_email`**, and **`mark_emails_read`** to Gemini's toolset:

```python
import os
import time
import html
import base64
import datetime
from email.message import EmailMessage
from dotenv import load_dotenv
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
CALENDAR_SERVICE = build("calendar", "v3", credentials=CREDS)
DRIVE_SERVICE = build("drive", "v3", credentials=CREDS)
TASKS_SERVICE = build("tasks", "v1", credentials=CREDS)


def build_html_email(clean_body: str) -> str:
    """Clean, native-looking editorial HTML that reads like a genuine 1-to-1 human email."""
    paragraphs = [p.strip() for p in clean_body.split("\n\n") if p.strip()]
    html_paragraphs = "".join(
        f'<p style="margin: 0 0 16px 0; line-height: 1.65; color: #1f2328; font-size: 15px;">'
        f'{html.escape(p).replace(chr(10), "<br>")}</p>'
        for p in paragraphs
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
              {html_paragraphs}
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
    """Extracts plain-text body content from a Gmail message payload."""
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


def get_emails(query: str = "", max_results: int = 5) -> list[dict]:
    """Fetches and reads emails from Gmail using optional Gmail search queries.

    Args:
        query: Gmail search filter (e.g., 'newer_than:1d' for today's mail, 'is:unread', 'from:gedhiraj@gmail.com', or '' for latest emails).
        max_results: Number of emails to retrieve (default 5, max 15).
    """
    max_results = min(max(1, max_results), 15)
    response = GMAIL_SERVICE.users().messages().list(userId="me", q=query, maxResults=max_results).execute()
    messages = response.get("messages", [])

    if not messages:
        return [{"info": f"No emails found matching query: '{query}'"}]

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
            "body": body_text[:2500],  # Trim overly long newsletters
        })
    return results


def send_email(to_email: str, subject: str, body: str) -> str:
    """Drafts and sends a new clean HTML + plain-text email via Gmail.

    Args:
        to_email: The recipient's email address.
        subject: A concise, natural, sentence-case subject line.
        body: Concise, polite, natural body text (do NOT include sign-off; the HTML template adds it automatically).
    """
    clean_body = body.replace("\\n", "\n").strip()
    clean_subject = subject.strip()

    print("\n" + "=" * 55, flush=True)
    print("EMAIL DRAFT PREVIEW", flush=True)
    print("=" * 55, flush=True)
    print(f"To:      {to_email}", flush=True)
    print(f"Subject: {clean_subject}", flush=True)
    print("-" * 55, flush=True)
    print(clean_body, flush=True)
    print("\nDhiraj Kumar", flush=True)
    print("=" * 55, flush=True)

    confirm = input("Approve and send this email? (y/n): ").strip().lower()
    if confirm != "y":
        return "Email cancelled by user. Ask if they want any edits."

    message = EmailMessage()
    message["To"] = to_email
    message["Subject"] = clean_subject
    message.set_content(f"{clean_body}\n\nDhiraj Kumar")
    message.add_alternative(build_html_email(clean_body), subtype="html")

    encoded_message = base64.urlsafe_b64encode(message.as_bytes()).decode()
    GMAIL_SERVICE.users().messages().send(userId="me", body={"raw": encoded_message}).execute()

    print("-> Email sent via Gmail API!", flush=True)
    return f"Email successfully sent to {to_email} with subject '{clean_subject}'."


def reply_to_email(message_id: str, body: str) -> str:
    """Replies directly inside an existing Gmail thread after user approval.

    Args:
        message_id: The Gmail message 'id' returned by `get_emails` that you are replying to.
        body: Concise, polite reply text (do NOT include sign-off).
    """
    orig = GMAIL_SERVICE.users().messages().get(userId="me", id=message_id, format="metadata").execute()
    headers = orig.get("payload", {}).get("headers", [])
    orig_from = next((h["value"] for h in headers if h["name"] == "From"), "")
    orig_subject = next((h["value"] for h in headers if h["name"] == "Subject"), "")
    orig_msg_id = next((h["value"] for h in headers if h["name"] == "Message-ID"), "")
    thread_id = orig.get("threadId")

    reply_subject = orig_subject if orig_subject.lower().startswith("re:") else f"Re: {orig_subject}"
    clean_body = body.replace("\\n", "\n").strip()

    print("\n" + "=" * 55, flush=True)
    print("REPLY DRAFT PREVIEW", flush=True)
    print("=" * 55, flush=True)
    print(f"To:      {orig_from}", flush=True)
    print(f"Subject: {reply_subject}", flush=True)
    print("-" * 55, flush=True)
    print(clean_body, flush=True)
    print("\nDhiraj Kumar", flush=True)
    print("=" * 55, flush=True)

    confirm = input("Approve and send this reply? (y/n): ").strip().lower()
    if confirm != "y":
        return "Reply cancelled by user."

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

    print("-> Reply sent in thread!", flush=True)
    return f"Reply sent to {orig_from} on thread '{reply_subject}'."


def mark_emails_read(message_ids: list[str]) -> str:
    """Marks one or more Gmail messages as read.

    Args:
        message_ids: List of Gmail message IDs to mark as read.
    """
    if not message_ids:
        return "No message IDs provided."
    GMAIL_SERVICE.users().messages().batchModify(
        userId="me",
        body={"ids": message_ids, "removeLabelIds": ["UNREAD"]},
    ).execute()
    return f"Marked {len(message_ids)} email(s) as read."


def send_with_fallback(ai_client: genai.Client, system_prompt: str, history: list, user_input: str):
    last_error = None
    for model_name in FALLBACK_MODELS:
        try:
            chat = ai_client.chats.create(
                model=model_name,
                history=history,
                config=types.GenerateContentConfig(
                    system_instruction=system_prompt,
                    tools=[get_emails, send_email, reply_to_email, mark_emails_read],
                ),
            )
            reply = chat.send_message(user_input)
            return reply.text, chat.get_history()
        except Exception as e:
            last_error = e
            if "503" in str(e) or "UNAVAILABLE" in str(e):
                print(f"({model_name} busy, switching model...)", flush=True)
                time.sleep(1)
                continue
            raise e
    raise last_error


def main():
    ai_client = genai.Client(api_key=GEMINI_API_KEY)
    today_str = datetime.date.today().strftime("%Y/%m/%d")

    system_prompt = f"""
    You are D25, the personal AI assistant for Dhiraj Kumar. Today's date is {today_str}.

    GMAIL CAPABILITIES & RULES:
    - Use `get_emails` whenever the user asks to read, search, or summarize emails.
      * For "today's emails", pass `query="after:{today_str}"` (or `query="newer_than:1d"`).
      * For "last 5 emails", pass `query=""` and `max_results=5`.
      * For unread emails, pass `query="is:unread"`.
    - When summarizing emails, highlight the sender, core topic, and any deadlines or action items in crisp bullet points.
    - When sending (`send_email`) or replying (`reply_to_email`), write like a natural, polite human—no AI clichés ("I hope this email finds you well", "pleased to confirm", etc.), use natural contractions, keep it concise to the user's exact points, and NEVER add "Dhiraj Kumar" at the end of `body` (the HTML template adds it automatically).
    """

    history = []
    print("D25 Gmail Assistant is ready! Type 'exit' to quit.", flush=True)
    while True:
        user_input = input("\nYou: ").strip()
        if user_input.lower() in {"exit", "quit"}:
            break
        if not user_input:
            continue

        print("D25 is thinking...", flush=True)
        try:
            reply_text, history = send_with_fallback(ai_client, system_prompt, history, user_input)
            print(f"\nD25: {reply_text}", flush=True)
        except Exception as e:
            print(f"\n[Error]: {e}", flush=True)


if __name__ == "__main__":
    main()

```

### Try These Prompts Right Away:

1. `"Summarize my last 5 emails with the key points."`
2. `"Summarize all emails I received today and highlight anything urgent."`
3. `"Check my unread emails and mark the promotional ones as read."`
4. `"Reply to the latest email from gedhiraj@gmail.com saying thanks, I got your reply."`

The web chatbot can also work with Google Calendar in the conversation. Try asking it to show upcoming events, schedule a meeting, reschedule an event, or cancel one. Calendar changes are previewed in the chat and require your approval before guests are notified.
