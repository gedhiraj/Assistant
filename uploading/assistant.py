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


def build_html_email(subject: str, clean_body: str) -> str:
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


def send_email(to_email: str, subject: str, body: str) -> str:
    """Drafts and sends a clean HTML + plain-text email via Gmail.

    Args:
        to_email: The recipient's email address.
        subject: A concise, natural, sentence-case subject line.
        body: Concise, polite, natural body text (do NOT include sign-off; the HTML template adds it automatically).
    """
    clean_body = body.replace("\\n", "\n").strip()
    clean_subject = subject.strip()

    print("\n" + "=" * 55, flush=True)
    print("EMAIL DRAFT PREVIEW (Human-Style Editorial HTML)", flush=True)
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

    # Plain-text fallback + Styled HTML version
    plain_full = f"{clean_body}\n\nDhiraj Kumar"
    html_full = build_html_email(clean_subject, clean_body)

    message.set_content(plain_full)
    message.add_alternative(html_full, subtype="html")

    encoded_message = base64.urlsafe_b64encode(message.as_bytes()).decode()
    GMAIL_SERVICE.users().messages().send(userId="me", body={"raw": encoded_message}).execute()

    print("-> Email sent via Gmail API!", flush=True)
    return f"Email successfully sent to {to_email} with subject '{clean_subject}'."


def fetch_workspace_context() -> dict:
    msg_list = GMAIL_SERVICE.users().messages().list(userId="me", maxResults=5).execute().get("messages", [])
    emails = []
    for m in msg_list:
        detail = GMAIL_SERVICE.users().messages().get(userId="me", id=m["id"], format="metadata").execute()
        headers = detail.get("payload", {}).get("headers", [])
        subject = next((h["value"] for h in headers if h["name"] == "Subject"), "(No Subject)")
        sender = next((h["value"] for h in headers if h["name"] == "From"), "Unknown")
        emails.append(f"From: {sender} | Subject: {subject}")

    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
    events_raw = (
        CALENDAR_SERVICE.events()
        .list(calendarId="primary", timeMin=now_iso, maxResults=5, singleEvents=True, orderBy="startTime")
        .execute()
        .get("items", [])
    )
    events = [f"{e['start'].get('dateTime', e['start'].get('date'))} -> {e.get('summary', 'Untitled')}" for e in events_raw]

    tasks_raw = TASKS_SERVICE.tasks().list(tasklist="@default", showCompleted=False, maxResults=10).execute().get("items", [])
    todo_items = [t.get("title", "Untitled Task") for t in tasks_raw]

    return {"emails": emails, "events": events, "tasks": todo_items}


def send_with_fallback(ai_client: genai.Client, system_prompt: str, history: list, user_input: str):
    last_error = None
    for model_name in FALLBACK_MODELS:
        try:
            chat = ai_client.chats.create(
                model=model_name,
                history=history,
                config=types.GenerateContentConfig(
                    system_instruction=system_prompt,
                    tools=[send_email],
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
    print("Fetching your latest Workspace data...", flush=True)
    context = fetch_workspace_context()

    ai_client = genai.Client(api_key=GEMINI_API_KEY)

    system_prompt = f"""
    You are D25, the personal AI assistant for Dhiraj Kumar.

    HUMAN-LIKE EMAIL WRITING RULES:
    Whenever the user asks to send an email, call `send_email` directly.
    1. Sound completely human, natural, and polite—never robotic or overly formal.
    2. NEVER use AI clichés such as "I hope this email finds you well", "I am writing to inform you", "pleased to confirm", or "at your earliest convenience".
    3. Use natural contractions (e.g., "I'm", "it's", "let's") and keep the message concise—stick strictly to the user's exact points without inventing extra details unless asked to explain.
    4. Write a natural, sentence-case subject line (e.g., "Quick check on email automation").
    5. Do NOT add a sign-off ("Best regards, Dhiraj Kumar") at the end of `body`—the HTML template adds Dhiraj's signature automatically.

    Live Google Workspace snapshot:
    - Recent Emails: {context['emails']}
    - Upcoming Calendar Events: {context['events']}
    - Pending Tasks: {context['tasks']}
    """

    history = []
    print("\nD25 is ready! Type 'exit' to quit.", flush=True)
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