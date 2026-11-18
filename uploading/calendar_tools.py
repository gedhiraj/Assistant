from datetime import date, datetime, timedelta
import re
from typing import Optional

from fastapi import HTTPException
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from pydantic import BaseModel, Field


class CalendarEventInput(BaseModel):
    summary: str = Field(min_length=1, max_length=250)
    description: str = Field(default="", max_length=8000)
    location: str = Field(default="", max_length=500)
    attendees: list[str] = Field(default_factory=list)
    reminder_minutes: Optional[int] = Field(default=15, ge=0, le=40320)
    use_default_reminders: bool = False
    all_day: bool = False
    start: Optional[datetime] = None
    end: Optional[datetime] = None
    start_date: Optional[date] = None
    end_date: Optional[date] = None


def calendar_service():
    from backend import get_credentials_for_active_user

    user_email, credentials = get_credentials_for_active_user()
    if not user_email or not credentials:
        raise HTTPException(status_code=401, detail="Sign in with Google to use Calendar.")
    return build("calendar", "v3", credentials=credentials)


def calendar_api_error(error: HttpError) -> HTTPException:
    status_code = int(getattr(error.resp, "status", 502))
    if status_code == 403:
        detail = "Google Calendar access was denied. Reconnect Google and grant Calendar permission."
    elif status_code == 404:
        detail = "The calendar event was not found."
    else:
        detail = "Google Calendar could not complete the request."
    if status_code < 400 or status_code > 599:
        status_code = 502
    return HTTPException(status_code=status_code, detail=detail)


def calendar_attendees(raw_emails: list[str]) -> list[dict]:
    if len(raw_emails) > 50:
        raise HTTPException(status_code=422, detail="An event can have at most 50 guests.")

    attendees = []
    seen_attendees = set()
    for raw_email in raw_emails:
        email = raw_email.strip()
        if not re.fullmatch(r"[^@\s,;<>]+@[^@\s,;<>]+\.[^@\s,;<>]+", email):
            raise HTTPException(status_code=422, detail=f"Invalid guest email address: {email or '(empty)'}.")
        key = email.casefold()
        if key not in seen_attendees:
            attendees.append({"email": email})
            seen_attendees.add(key)
    return attendees


def calendar_event_body(payload: CalendarEventInput) -> dict:
    summary = payload.summary.strip()
    if not summary:
        raise HTTPException(status_code=422, detail="Event title cannot be empty.")
    attendees = calendar_attendees(payload.attendees)

    if payload.all_day:
        if not payload.start_date or not payload.end_date or payload.start or payload.end:
            raise HTTPException(status_code=422, detail="Choose a start and end date for an all-day event.")
        if payload.end_date < payload.start_date:
            raise HTTPException(status_code=422, detail="Event end date must be on or after its start date.")
        start = {"date": payload.start_date.isoformat()}
        end = {"date": (payload.end_date + timedelta(days=1)).isoformat()}
    else:
        if not payload.start or not payload.end or payload.start_date or payload.end_date:
            raise HTTPException(status_code=422, detail="Choose a start and end time for the event.")
        if payload.start.utcoffset() is None or payload.end.utcoffset() is None:
            raise HTTPException(status_code=422, detail="Event times must include a timezone.")
        if payload.end <= payload.start:
            raise HTTPException(status_code=422, detail="Event end time must be after its start time.")
        start = {"dateTime": payload.start.isoformat()}
        end = {"dateTime": payload.end.isoformat()}

    reminders = (
        {"useDefault": True}
        if payload.use_default_reminders
        else {
            "useDefault": False,
            "overrides": (
                [{"method": "popup", "minutes": payload.reminder_minutes}]
                if payload.reminder_minutes is not None
                else []
            ),
        }
    )

    return {
        "summary": summary,
        "description": payload.description.strip(),
        "location": payload.location.strip(),
        "start": start,
        "end": end,
        "attendees": attendees,
        "reminders": reminders,
    }


def public_calendar_event(event: dict) -> dict:
    return {
        "id": event.get("id"),
        "summary": event.get("summary", "(Untitled event)"),
        "description": event.get("description", ""),
        "location": event.get("location", ""),
        "start": event.get("start", {}),
        "end": event.get("end", {}),
        "attendees": [
            {
                "email": attendee.get("email", ""),
                "responseStatus": attendee.get("responseStatus", "needsAction"),
            }
            for attendee in event.get("attendees", [])
        ],
        "reminders": event.get("reminders", {}),
        "htmlLink": event.get("htmlLink", ""),
    }


def parse_calendar_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.astimezone() if parsed.utcoffset() is None else parsed


def parse_calendar_date(value: str) -> date:
    return date.fromisoformat(value)
