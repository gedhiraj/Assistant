import os
import datetime
from dotenv import load_dotenv
from google import genai
from google.genai import types
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

# 1. Load environment variables from .env
load_dotenv()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
CREDENTIALS_FILE = os.getenv("GOOGLE_CREDENTIALS_FILE", "credentials.json")
TOKEN_FILE = os.getenv("GOOGLE_TOKEN_FILE", "token.json")

if not GEMINI_API_KEY:
    raise ValueError("Missing GEMINI_API_KEY in .env file.")

# 2. All Google Workspace scopes for your D25 assistant
# If you add/remove scopes later, delete token.json and re-run once.
SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/tasks",
    "https://www.googleapis.com/auth/contacts.readonly",
]


def get_google_credentials() -> Credentials:
    """Authenticates via OAuth 2.0 and caches session in token.json."""
    creds = None
    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not os.path.exists(CREDENTIALS_FILE):
                raise FileNotFoundError(
                    f"'{CREDENTIALS_FILE}' not found. Place your downloaded OAuth JSON in this folder."
                )
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
            creds = flow.run_local_server(port=0)

        with open(TOKEN_FILE, "w") as token:
            token.write(creds.to_json())

    return creds


def fetch_workspace_context(creds: Credentials) -> dict:
    """Pulls recent data across Gmail, Calendar, Drive, and Tasks."""
    gmail = build("gmail", "v1", credentials=creds)
    calendar = build("calendar", "v3", credentials=creds)
    drive = build("drive", "v3", credentials=creds)
    tasks = build("tasks", "v1", credentials=creds)

    # 1. Latest 5 Gmail messages
    msg_list = gmail.users().messages().list(userId="me", maxResults=5).execute().get("messages", [])
    emails = []
    for m in msg_list:
        detail = gmail.users().messages().get(userId="me", id=m["id"], format="metadata").execute()
        headers = detail.get("payload", {}).get("headers", [])
        subject = next((h["value"] for h in headers if h["name"] == "Subject"), "(No Subject)")
        sender = next((h["value"] for h in headers if h["name"] == "From"), "Unknown")
        emails.append(f"From: {sender} | Subject: {subject}")

    # 2. Upcoming 5 Calendar events
    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
    events_raw = (
        calendar.events()
        .list(
            calendarId="primary",
            timeMin=now_iso,
            maxResults=5,
            singleEvents=True,
            orderBy="startTime",
        )
        .execute()
        .get("items", [])
    )
    events = [
        f"{e['start'].get('dateTime', e['start'].get('date'))} -> {e.get('summary', 'Untitled')}"
        for e in events_raw
    ]

    # 3. Recent 5 Drive files
    files_raw = drive.files().list(pageSize=5, fields="files(name, mimeType)").execute().get("files", [])
    files = [f"{f['name']} ({f['mimeType']})" for f in files_raw]

    # 4. Pending Google Tasks
    tasks_raw = tasks.tasks().list(tasklist="@default", showCompleted=False, maxResults=10).execute().get("items", [])
    todo_items = [t.get("title", "Untitled Task") for t in tasks_raw]

    return {
        "emails": emails,
        "events": events,
        "files": files,
        "tasks": todo_items,
    }


def main():
    print("Authenticating Google Workspace services...")
    creds = get_google_credentials()

    print("Fetching your latest Workspace data...")
    context = fetch_workspace_context(creds)

    # Initialize Gemini client using key from .env
    ai_client = genai.Client(
        api_key=GEMINI_API_KEY,
        http_options=types.HttpOptions(api_version="v1beta"),
    )

    system_prompt = f"""
    You are D25, a personal AI assistant connected to the user's Google account.
    Here is the user's live Google Workspace snapshot:
    - Recent Emails: {context['emails']}
    - Upcoming Calendar Events: {context['events']}
    - Pending Tasks: {context['tasks']}
    - Recent Drive Files: {context['files']}
    """

    # Start an interactive chat session grounded in your Google data
    chat = ai_client.chats.create(
        model="gemini-flash-latest",
        config=types.GenerateContentConfig(system_instruction=system_prompt),
    )

    briefing = chat.send_message("Give me a concise briefing of my emails, schedule, and pending tasks.")
    print("\n--- D25 Daily Briefing ---")
    print(briefing.text)

    print("\nAsk D25 anything about your workspace (type 'exit' to quit):")
    while True:
        user_input = input("\nYou: ").strip()
        if user_input.lower() in {"exit", "quit"}:
            break
        if not user_input:
            continue
        reply = chat.send_message(user_input)
        print(f"\nD25: {reply.text}")


if __name__ == "__main__":
    main()