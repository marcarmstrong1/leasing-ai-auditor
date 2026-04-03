"""
Email Monitor - Gmail API layer for persona inbox management.

Handles:
- Sending the post-handoff follow-up email from the persona
- Monitoring the inbox for human leasing agent replies
- Capturing precise response timestamps
- Threading detection (did they reply to our email or start fresh?)
- Logging all email messages to the database
"""

import base64
import time
from datetime import datetime, timezone
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from typing import Optional
from loguru import logger

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from config.settings import settings
from database.models import (
    Engagement, Message,
    MessageSender, ConversationStage, ChannelType,
    EngagementStatus
)
from database.connection import get_db


# Gmail API scope — read and send only
SCOPES = [
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.readonly",
]

# How often to poll for new emails (seconds)
POLL_INTERVAL = 300  # Every 5 minutes

# Labels that indicate a reply came from a real human
# (as opposed to auto-responders or marketing)
AUTO_RESPONDER_SIGNALS = [
    "auto-reply",
    "automatic reply",
    "out of office",
    "noreply",
    "no-reply",
    "donotreply",
    "do-not-reply",
    "mailer-daemon",
]


class EmailMonitor:
    def __init__(self, persona: dict, credentials_path: str):
        """
        persona: The persona dict (from personas/maya.json etc)
        credentials_path: Path to Gmail OAuth credentials JSON
        """
        self.persona = persona
        self.persona_email = persona["email"]
        self.credentials_path = credentials_path
        self.service = None
        logger.info(f"EmailMonitor initialized for {self.persona_email}")

    # -------------------------------------------------------------------------
    # AUTH
    # -------------------------------------------------------------------------

    def authenticate(self):
        """
        Authenticates with Gmail API using OAuth2.
        On Cloud Run, credentials are pulled from Secret Manager.
        Locally, reads from credentials_path.
        """
        import os
        import json

        creds = None

        # Try loading saved token first
        token_path = self.credentials_path.replace(
            "credentials.json", f"token_{self.persona['id']}.json"
        )

        if os.path.exists(token_path):
            creds = Credentials.from_authorized_user_file(token_path, SCOPES)

        # Refresh or re-authenticate if needed
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                from google_auth_oauthlib.flow import InstalledAppFlow
                flow = InstalledAppFlow.from_client_secrets_file(
                    self.credentials_path, SCOPES
                )
                creds = flow.run_local_server(port=0)

            # Save token for next run
            with open(token_path, "w") as f:
                f.write(creds.to_json())

        self.service = build("gmail", "v1", credentials=creds)
        logger.success(f"Gmail API authenticated for {self.persona_email}")

    # -------------------------------------------------------------------------
    # SENDING
    # -------------------------------------------------------------------------

    def send_followup_email(
        self,
        engagement_id: str,
        to_address: str,
        property_name: str,
        webchat_summary: str,
    ) -> Optional[str]:
        """
        Sends the post-handoff follow-up email from the persona.
        This is triggered after the webchat handoff stage completes.

        Returns the Gmail message ID if sent successfully, None if failed.
        """
        subject, body = self._compose_followup(property_name, webchat_summary)

        message = MIMEMultipart()
        message["to"] = to_address
        message["from"] = self.persona_email
        message["subject"] = subject
        message.attach(MIMEText(body, "plain"))

        raw = base64.urlsafe_b64encode(message.as_bytes()).decode()

        try:
            sent = self.service.users().messages().send(
                userId="me",
                body={"raw": raw}
            ).execute()

            message_id = sent["id"]
            sent_at = datetime.now(timezone.utc)

            logger.success(
                f"Follow-up email sent for engagement {engagement_id} "
                f"to {to_address} | Gmail ID: {message_id}"
            )

            # Save to DB
            self._save_email_message(
                engagement_id=engagement_id,
                sender=MessageSender.PERSONA,
                content=body,
                subject=subject,
                thread_id=sent.get("threadId"),
                sent_at=sent_at,
                stage=ConversationStage.HANDOFF_TRIGGER,
            )

            return message_id

        except HttpError as e:
            logger.error(f"Failed to send follow-up email: {e}")
            return None

    def _compose_followup(
        self,
        property_name: str,
        webchat_summary: str
    ) -> tuple[str, str]:
        """
        Composes a natural follow-up email from the persona.
        Uses the webchat summary as context for continuity.
        """
        subject = f"Following up on my inquiry — {self.persona['name']}"

        body = f"""Hi,

I was just chatting on your website about apartments at {property_name} and the assistant suggested I reach out directly for a few more specific questions.

Quick background on my situation: {self._persona_brief()}

I had a few questions I'd love to talk through before scheduling a tour:

{self._persona_questions()}

I'm hoping to make a decision in the next couple of weeks so any help is appreciated. You can reach me at this email or feel free to call/text me if that's easier.

Thanks so much,
{self.persona['name']}
""".strip()

        return subject, body

    def _persona_brief(self) -> str:
        """One-sentence summary of the persona's situation."""
        briefs = {
            "maya": (
                "I'm relocating for a new job and looking for a 1-bedroom, "
                f"ideally in the ${self.persona['budget_min']}-"
                f"${self.persona['budget_max']}/month range with a "
                f"{self.persona['timeline']} timeline."
            ),
            "garcia": (
                "My husband and I are relocating and need a 2-bedroom. "
                "We have a 65lb Labrador so pet policy is really important to us, "
                f"and we're working with a {self.persona['timeline']} timeline."
            ),
        }
        return briefs.get(
            self.persona["id"],
            f"I'm looking for a {self.persona['unit_preference']} "
            f"with a {self.persona['timeline']} move-in timeline."
        )

    def _persona_questions(self) -> str:
        """2-3 natural follow-up questions from the persona."""
        questions = {
            "maya": (
                "- Are there any current move-in specials or concessions available?\n"
                "- What internet providers service the building? "
                "I work from home full time so reliable speeds are a must.\n"
                "- Is there flexibility on the lease start date if I'm between two options?"
            ),
            "garcia": (
                "- Can you confirm the exact pet deposit and monthly pet rent for a 65lb dog?\n"
                "- Is the pet deposit fully refundable at move-out?\n"
                "- Are any of the available 2BR units on the ground floor or in "
                "a building with an elevator?"
            ),
        }
        return questions.get(
            self.persona["id"],
            "- Can you tell me more about current availability and pricing?\n"
            "- Are there any move-in specials right now?\n"
            "- What is the application process like?"
        )

    # -------------------------------------------------------------------------
    # MONITORING
    # -------------------------------------------------------------------------

    def check_for_replies(
        self,
        engagement_id: str,
        sent_message_id: str,
        handoff_triggered_at: datetime,
    ) -> Optional[dict]:
        """
        Checks the inbox for replies to our follow-up email.
        Returns the reply data if found, None if not yet received.

        Call this on a schedule (every POLL_INTERVAL seconds).
        """
        try:
            # Search for messages in our inbox after the handoff time
            query = (
                f"to:{self.persona_email} "
                f"after:{int(handoff_triggered_at.timestamp())}"
            )

            results = self.service.users().messages().list(
                userId="me",
                q=query,
                maxResults=10,
            ).execute()

            messages = results.get("messages", [])

            if not messages:
                logger.debug(f"No replies yet for engagement {engagement_id}")
                return None

            for msg_ref in messages:
                msg_data = self.service.users().messages().get(
                    userId="me",
                    id=msg_ref["id"],
                    format="full",
                ).execute()

                reply = self._parse_email_message(msg_data)

                if not reply:
                    continue

                # Skip auto-responders
                if self._is_auto_responder(reply):
                    logger.info(
                        f"Skipping auto-responder from {reply.get('from_address')}"
                    )
                    continue

                # This is a real human reply
                received_at = reply["received_at"]
                minutes_elapsed = (
                    received_at - handoff_triggered_at
                ).total_seconds() / 60

                logger.success(
                    f"Human reply received for engagement {engagement_id} | "
                    f"{minutes_elapsed:.0f} minutes after handoff"
                )

                # Save to DB
                self._save_email_message(
                    engagement_id=engagement_id,
                    sender=MessageSender.HUMAN_LEASING,
                    content=reply["body"],
                    subject=reply["subject"],
                    thread_id=reply.get("thread_id"),
                    sent_at=received_at,
                    stage=ConversationStage.HUMAN_FOLLOWUP,
                )

                # Update engagement record
                with get_db() as db:
                    engagement = db.query(Engagement).filter_by(
                        id=engagement_id
                    ).first()
                    if engagement:
                        engagement.first_human_response_at = received_at
                        engagement.minutes_to_first_human_response = minutes_elapsed
                        engagement.status = EngagementStatus.SCORING

                        # Rough heuristic: did they mention anything
                        # from the original chat?
                        engagement.human_had_context = self._detect_context_continuity(
                            reply["body"]
                        )

                return reply

        except HttpError as e:
            logger.error(f"Gmail API error checking replies: {e}")
            return None

    def wait_for_reply(
        self,
        engagement_id: str,
        sent_message_id: str,
        handoff_triggered_at: datetime,
        max_wait_hours: int = None,
    ) -> Optional[dict]:
        """
        Polls for a reply until one arrives or max_wait_hours is exceeded.
        Designed to run as a background process or Cloud Run job.

        Returns the reply dict or None if timed out.
        """
        max_wait_hours = max_wait_hours or settings.handoff_wait_hours
        max_seconds = max_wait_hours * 3600
        elapsed = 0

        logger.info(
            f"Monitoring inbox for engagement {engagement_id} "
            f"(max wait: {max_wait_hours}hrs)"
        )

        while elapsed < max_seconds:
            reply = self.check_for_replies(
                engagement_id=engagement_id,
                sent_message_id=sent_message_id,
                handoff_triggered_at=handoff_triggered_at,
            )

            if reply:
                return reply

            logger.debug(
                f"No reply yet. Elapsed: {elapsed/3600:.1f}hrs. "
                f"Polling again in {POLL_INTERVAL/60:.0f} min."
            )
            time.sleep(POLL_INTERVAL)
            elapsed += POLL_INTERVAL

        # Timed out — mark engagement accordingly
        logger.warning(
            f"No human reply received within {max_wait_hours}hrs "
            f"for engagement {engagement_id}"
        )

        with get_db() as db:
            engagement = db.query(Engagement).filter_by(id=engagement_id).first()
            if engagement:
                engagement.status = EngagementStatus.SCORING
                engagement.minutes_to_first_human_response = None

        return None

    # -------------------------------------------------------------------------
    # PARSING
    # -------------------------------------------------------------------------

    def _parse_email_message(self, msg_data: dict) -> Optional[dict]:
        """
        Extracts structured data from a raw Gmail API message.
        Returns a clean dict with from, subject, body, and timestamp.
        """
        try:
            headers = {
                h["name"].lower(): h["value"]
                for h in msg_data["payload"]["headers"]
            }

            from_address = headers.get("from", "")
            subject = headers.get("subject", "")
            date_str = headers.get("date", "")

            # Internal timestamp (milliseconds since epoch)
            internal_date_ms = int(msg_data.get("internalDate", 0))
            received_at = datetime.fromtimestamp(
                internal_date_ms / 1000, tz=timezone.utc
            )

            # Extract body
            body = self._extract_body(msg_data["payload"])

            if not body:
                return None

            return {
                "id": msg_data["id"],
                "thread_id": msg_data.get("threadId"),
                "from_address": from_address,
                "subject": subject,
                "body": body,
                "received_at": received_at,
            }

        except Exception as e:
            logger.warning(f"Failed to parse email message: {e}")
            return None

    def _extract_body(self, payload: dict) -> str:
        """
        Recursively extracts plain text body from Gmail payload.
        Handles simple messages and multipart/alternative structures.
        """
        mime_type = payload.get("mimeType", "")

        if mime_type == "text/plain":
            data = payload.get("body", {}).get("data", "")
            if data:
                return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")

        if mime_type.startswith("multipart"):
            for part in payload.get("parts", []):
                text = self._extract_body(part)
                if text:
                    return text

        return ""

    # -------------------------------------------------------------------------
    # HEURISTICS
    # -------------------------------------------------------------------------

    def _is_auto_responder(self, reply: dict) -> bool:
        """
        Detects auto-reply and out-of-office messages.
        We don't want these counted as human responses.
        """
        from_addr = reply.get("from_address", "").lower()
        subject = reply.get("subject", "").lower()
        body = reply.get("body", "").lower()

        for signal in AUTO_RESPONDER_SIGNALS:
            if signal in from_addr or signal in subject or signal in body[:200]:
                return True
        return False

    def _detect_context_continuity(self, reply_body: str) -> bool:
        """
        Rough heuristic: did the human reply reference anything specific
        from the original chatbot conversation?

        Looks for persona-specific keywords that would only be present
        if the leasing agent had read the prior chat.
        """
        body_lower = reply_body.lower()

        # Keywords that suggest context was passed through
        context_signals = [
            self.persona.get("unit_preference", "").lower(),
            self.persona.get("special_needs", "").lower(),
            "work from home", "relocat", "pet", "dog",
            "move-in special", "concession",
            "timeline", "60 day", "45 day",
        ]

        matches = sum(1 for signal in context_signals if signal and signal in body_lower)
        return matches >= 2  # Two or more signals = likely had context

    # -------------------------------------------------------------------------
    # DATABASE
    # -------------------------------------------------------------------------

    def _save_email_message(
        self,
        engagement_id: str,
        sender: MessageSender,
        content: str,
        subject: str,
        thread_id: Optional[str],
        sent_at: datetime,
        stage: ConversationStage,
    ):
        with get_db() as db:
            msg = Message(
                engagement_id=engagement_id,
                sender=sender,
                channel=ChannelType.EMAIL,
                stage=stage,
                content=content,
                email_subject=subject,
                email_thread_id=thread_id,
                sent_at=sent_at,
            )
            db.add(msg)
        logger.debug(f"Email message saved: {sender} | {subject[:50]}")
