# D25 – AI-Powered Google Workspace Assistant

An intelligent AI-powered personal productivity assistant that integrates **Gmail, Google Calendar, and Google Sheets** into a unified conversational interface.

D25 helps users manage emails, organize schedules, work with spreadsheets, and automate everyday productivity tasks using natural language.

The goal is to build a unified AI workspace where users can interact with multiple Google services through a single chatbot interface.

---

## 1. Project Overview

D25 is designed to simplify everyday productivity by bringing essential Google Workspace services together.

Instead of switching between Gmail, Calendar, and Sheets, users can simply communicate with D25 to perform tasks, retrieve information, and manage their workflows.

### Core Objectives

- Simplify email management through natural language.
- Automate calendar and scheduling operations.
- Interact with Google Sheets without manually navigating spreadsheets.
- Enable cross-application workflows.
- Provide a conversational AI experience with contextual understanding.
- Maintain user control through approval-based actions.

---

## 2. Current Features and Capabilities

### 2.1 Gmail Integration

D25 provides email management capabilities through Gmail.

**Implemented functionality:**

- Search emails in the Primary Inbox.
- Retrieve and summarize email conversations.
- Draft new emails.
- Send emails with user approval.
- Reply to existing email threads.
- Mark emails as read or unread.
- Archive emails.
- Move emails to Trash.
- Preview email content within the chat interface.
- Trusted-recipient management for streamlined sending approvals.

**Example commands:**

- "Show my latest unread emails."
- "Summarize my recent emails."
- "Draft a follow-up email to HR."
- "Reply to this email confirming my availability."
- "Archive the completed conversations."

### 2.2 Google Calendar Integration

D25 allows users to manage their schedules conversationally.

**Implemented functionality:**

- Retrieve upcoming calendar events.
- Search for existing events.
- Create calendar events.
- Update existing events.
- Cancel calendar events.
- Approval-based calendar modifications.

**Example commands:**

- "Show my meetings for tomorrow."
- "What is my schedule this week?"
- "Schedule a meeting for Friday at 3 PM."
- "Reschedule my afternoon meeting."
- "Cancel the meeting scheduled for tomorrow."

### 2.3 Google Sheets Integration

D25 provides spreadsheet management through natural language.

**Implemented functionality:**

- Find Google spreadsheets.
- Read spreadsheet data.
- Create new spreadsheets.
- Edit spreadsheet values.
- Create and manage spreadsheet tabs.
- Format spreadsheet cells.
- Sort spreadsheet data.
- Apply filters.
- Generate charts.
- Export spreadsheet files.
- Manage sharing permissions for named users.
- Open and download spreadsheet files.

**Example commands:**

- "Find my monthly expense spreadsheet."
- "Create a new sales tracking sheet."
- "Add these records to the spreadsheet."
- "Sort the data by revenue."
- "Create a chart showing monthly sales."
- "Export this spreadsheet."

---

## 3. Chat Interface and User Experience

D25 includes a conversational interface designed for everyday productivity.

**Current UI features:**

- Responsive chatbot interface.
- Streaming AI responses.
- Activity indicators and execution steps.
- Persistent chat sessions.
- Saved conversation history.
- Google account switching.
- Stop-response functionality.
- Email preview cards.
- Approval cards for sensitive operations.
- Trusted-recipient settings.
- Spreadsheet Open and Download actions.

### Action Approval System

D25 uses an approval-based approach for sensitive operations.

Examples include:
- Sending emails.
- Modifying calendar events.
- Performing mailbox changes.

This ensures that users remain in control of important operations.

---

## 4. Technical Architecture

D25 is organized around a conversational AI backend, Google Workspace integrations, and a web-based frontend.

### Main Components

| Component | Responsibility |
|---|---|
| `backend.py` | Main application backend, AI orchestration, tool execution and chat handling |
| `index.html` | Frontend interface and conversational experience |
| `calendar_tools.py` | Google Calendar integration |
| `sheets_tools.py` | Google Sheets operations |
| Gmail tools | Email retrieval, drafting, sending and mailbox management |
| OAuth | Google account authentication and authorization |

### Architecture Overview

User → Web Chat Interface → Backend → AI Model → Tool Selection → Google Workspace APIs → Results → Chat Interface

The backend coordinates requests, executes the relevant tools, handles approvals, and returns results to the user.

---

# 5. Planned Enhancements – Copilot-Inspired Experience

The next development phase focuses on making D25 more intelligent, contextual, proactive, and capable of handling multi-step workflows.

The inspiration comes from interaction patterns found in AI assistants such as GitHub Copilot, adapted for Google Workspace productivity rather than software development.

### 5.1 Ask / Plan / Act Modes

Introduce three interaction modes.

**ASK MODE**

For information retrieval and general questions.

Examples:
- "What meetings do I have tomorrow?"
- "Summarize my latest emails."
- "Find the latest sales report."

**PLAN MODE**

For planning complex operations before execution.

Examples:
- "Organize my schedule for next week."
- "Prepare a follow-up plan for pending emails."
- "Analyze my spreadsheet and suggest improvements."

The assistant should:
1. Understand the request.
2. Break it into actionable steps.
3. Identify the required Google services.
4. Present a proposed execution plan.
5. Wait for user confirmation where necessary.

**ACT MODE**

For executing approved workflows.

Examples:
- "Create calendar events based on these emails."
- "Send follow-up emails to the selected contacts."
- "Update my spreadsheet with the latest information."

The assistant executes the approved steps and reports the results.

### 5.2 Context Mentions (@Gmail, @Calendar, @Sheets)

Make the existing @ and & composer hints functional.

Allow users to explicitly select the context they want the AI to use.

Potential context options:

- `@Gmail` – Use email conversations and mailbox information.
- `@Calendar` – Use calendar events and schedules.
- `@Sheets` – Use spreadsheet data.
- `@Chat` – Reference saved conversations.

This will improve contextual accuracy and reduce unnecessary tool calls.

### 5.3 Google Tasks Integration

Introduce Google Tasks as another productivity integration.

**Planned functionality:**

- Create tasks.
- Retrieve task lists.
- Update tasks.
- Mark tasks as completed.
- Delete tasks.
- Set due dates.
- Organize tasks into lists.
- Track pending tasks and reminders.

Example:
"Create a task to follow up with HR tomorrow."

Note: The application already requests the Google Tasks OAuth scope, but dedicated Tasks tools still need to be implemented and integrated into the chat toolset.

### 5.4 Source Cards and Grounded Responses

Improve transparency by displaying the source behind AI-generated answers.

Examples:

- Email source cards with subject, sender and date.
- Calendar cards with event details.
- Spreadsheet cards with file names and relevant data.
- Direct links to the original Google Workspace resources.

This allows users to verify information without searching manually.

### 5.5 Saved Instructions and Preferences

Introduce persistent user preferences and instructions.

Examples:

- Preferred email writing tone.
- Default calendar.
- Timezone preferences.
- Frequently used recipients.
- Preferred response format.
- Personal productivity instructions.

Users should be able to view, update, and clear these preferences.

### 5.6 Cross-Application Workflows

Allow D25 to connect multiple Google services within one request.

Example workflow:

**Email → Calendar Event**

1. Read an email containing meeting details.
2. Extract the date, time and participants.
3. Prepare a calendar event.
4. Show the proposed event.
5. Request approval.
6. Create the event.
7. Confirm successful completion.

Other examples:

- Convert an email into a Google Task.
- Extract email attachments and organize information in Sheets.
- Generate a spreadsheet from email information.
- Create calendar reminders from pending tasks.
- Prepare follow-up emails based on previous conversations.

### 5.7 Action History and Undo

Introduce a centralized activity history.

Planned functionality:

- View previously executed actions.
- Track the date and time of operations.
- Display the Google service involved.
- Show execution status.
- Record user approvals.
- Provide undo functionality where supported by the underlying Google service.

---

# 6. Development Roadmap

| Priority | Feature | Status |
|---|---|---|
| 1 | Gmail Integration | Implemented |
| 2 | Google Calendar Integration | Implemented |
| 3 | Google Sheets Integration | Implemented |
| 4 | Approval-Based Actions | Implemented |
| 5 | Chat Session Management | Implemented |
| 6 | Ask / Plan / Act Modes | Planned |
| 7 | Functional Context Mentions | Planned |
| 8 | Source Cards | Planned |
| 9 | Google Tasks Integration | Planned |
| 10 | Saved User Preferences | Planned |
| 11 | Cross-App Workflows | Planned |
| 12 | Action History and Undo | Planned |

---

# 7. Future Vision

The long-term vision is to transform D25 from a conversational Google Workspace assistant into a comprehensive personal productivity agent.

Potential future capabilities include:

- Intelligent daily planning.
- Automated email follow-ups.
- Meeting preparation.
- Task prioritization.
- Spreadsheet analysis and reporting.
- Context-aware recommendations.
- Multi-step workflow automation.
- Personalized productivity insights.
- Proactive notifications and reminders.

The assistant should eventually understand the user's work context, identify pending actions, recommend next steps, and execute approved tasks across connected services.

---

# 8. Project Philosophy

D25 is built around four principles:

**1. Unified Experience**

Access multiple productivity tools from one conversational interface.

**2. Contextual Intelligence**

Understand user intent and retrieve relevant information from connected services.

**3. Controlled Automation**

Keep users informed and in control of sensitive actions.

**4. Practical Productivity**

Focus on reducing repetitive work and saving time through intelligent automation.

---

**D25 – One Assistant. Multiple Workspaces. Smarter Productivity.**
