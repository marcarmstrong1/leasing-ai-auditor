"""
Email Monitor - IMAP/SMTP layer for persona inbox management.
Uses Outlook accounts via standard IMAP/SMTP — no OAuth required.

Handles:
- Sending the post-handoff follow-up email from the persona
- Monitoring the inbox for human leasing agent replies
- Capturing precise response timestamps
- Auto-responder detection
- Context continuity heuristics
"""

import imaplib
import smtplib
import email
import time
from datetime import datetime, timezone
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.utils import parsedate_to_datetime
from typing import Optional
from loguru import logger

from config.settings import settings
from database.models import (
    Engagement, Message,
    MessageSender, ConversationStage, ChannelType,
    EngagementStatus
)
from database.connection import get_db


# --- Outlook IMAP/SMTP config ---
IMAP_SERVER = "outlook.office365.com"
IMAP_PORT   = 993
SMTP_SERVER = "smtp.office365.com"
SMTP_PORT   = 587

# How often to poll for new emails (seconds)
POLL_INTERVAL = 300  # Every 5 minutes

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
    def __init__(self, persona: dict, email_password: str):
        """
        persona      : The persona dict (from personas/maya.json etc)
        email_password: Password for the persona's Outlook account
        """
        self.persona       = persona
        self.persona_email = persona["email"]
        self.password      = email_password
        logger.info(f"EmailMonitor initialized for {self.persona_email}")

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
        Returns a message ID string if successful, None if failed.
        """
        subject, body = self._compose_followup(property_name, webchat_summary)

        msg = MIMEMultipart()
        msg["From"]    = self.persona_email
        msg["To"]      = to_address
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "plain"))

        try:
            with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
                server.ehlo()
                server.starttls()
                server.login(self.persona_email, self.password)
                server.sendmail(self.persona_email, to_address, msg.as_string())

            sent_at    = datetime.now(timezone.utc)
            message_id = f"{engagement_id}-{int(sent_at.timestamp())}"

            logger.success(
                f"Follow-up email sent for engagement {engagement_id} "
                f"to {to_address}"
            )

            self._save_email_message(
                engagement_id=engagement_id,
                sender=MessageSender.PERSONA,
                content=body,
                subject=subject,
                thread_id=None,
                sent_at=sent_at,
                stage=ConversationStage.HANDOFF_TRIGGER,
            )

            return message_id

        except Exception as e:
            logger.error(f"Failed to send follow-up email: {e}")
            return None

    def _compose_followup(
        self,
        property_name: str,
        webchat_summary: str
    ) -> tuple[str, str]:
        subject = f"Following up on my inquiry — {self.persona['name']}"
        body = f"""Hi,

I was just chatting on your website about apartments at {property_name} and the assistant suggested I reach out directly for a few more specific questions.

Quick background on my situation: {self._persona_brief()}

I had a few questions I'd love to talk through before scheduling a tour:

{self._persona_questions()}

I'm hoping to make a decision in the next couple of weeks so any help is appreciated. You can reach me at this email or feel free to call/text me if that's easier.

Thanks so much,
{self.persona['name']}""".strip()

        return subject, body

    def _persona_brief(self) -> str:
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
                "- Are any of the available 2BR units on the ground floor or "
                "in a building with an elevator?"
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
        handoff_triggered_at: datetime,
    ) -> Optional[dict]:
        """
        Connects to IMAP and scans inbox for replies since handoff time.
        Returns reply dict if a real human reply is found, None otherwise.
        """
        try:
            with imaplib.IMAP4_SSL(IMAP_SERVER, IMAP_PORT) as mail:
                mail.login(self.persona_email, self.password)
                mail.select("INBOX")

                # Search for unseen messages since handoff
                since_date = handoff_triggered_at.strftime("%d-%b-%Y")
                status, data = mail.search(None, f'(SINCE "{since_date}" UNSEEN)')

                if status != "OK" or not data[0]:
                    logger.debug(f"No new messages for engagement {engagement_id}")
                    return None

                message_ids = data[0].split()
                logger.debug(f"Found {len(message_ids)} new message(s) to check")

                for msg_id in message_ids:
                    status, msg_data = mail.fetch(msg_id, "(RFC822)")
                    if status != "OK":
                        continue

                    raw_email = msg_data[0][1]
                    parsed    = email.message_from_bytes(raw_email)
                    reply     = self._parse_imap_message(parsed)

                    if not reply:
                        continue

                    # Skip auto-responders
                    if self._is_auto_responder(reply):
                        logger.info(
                            f"Skipping auto-responder from "
                            f"{reply.get('from_address')}"
                        )
                        continue

                    # Check it arrived after handoff
                    if reply["received_at"] < handoff_triggered_at:
                        continue

                    received_at    = reply["received_at"]
                    minutes_elapsed = (
                        received_at - handoff_triggered_at
                    ).total_seconds() / 60

                    logger.success(
                        f"Human reply received for engagement {engagement_id} | "
                        f"{minutes_elapsed:.0f} minutes after handoff"
                    )

                    self._save_email_message(
                        engagement_id=engagement_id,
                        sender=MessageSender.HUMAN_LEASING,
                        content=reply["body"],
                        subject=reply["subject"],
                        thread_id=None,
                        sent_at=received_at,
                        stage=ConversationStage.HUMAN_FOLLOWUP,
                    )

                    with get_db() as db:
                        engagement = db.query(Engagement).filter_by(
                            id=engagement_id
                        ).first()
                        if engagement:
                            engagement.first_human_response_at  = received_at
                            engagement.minutes_to_first_human_response = minutes_elapsed
                            engagement.status                   = EngagementStatus.SCORING
                            engagement.human_had_context        = \
                                self._detect_context_continuity(reply["body"])

                    return reply

        except imaplib.IMAP4.error as e:
            logger.error(f"IMAP error: {e}")
        except Exception as e:
            logger.error(f"Unexpected error checking replies: {e}")

        return None

    def wait_for_reply(
        self,
        engagement_id: str,
        sent_message_id: str,
        handoff_triggered_at: datetime,
        max_wait_hours: int = None,
    ) -> Optional[dict]:
        """
        Polls inbox until a human reply arrives or max wait is exceeded.
        """
        max_wait_hours = max_wait_hours or settings.handoff_wait_hours
        max_seconds    = max_wait_hours * 3600
        elapsed        = 0

        logger.info(
            f"Monitoring inbox for engagement {engagement_id} "
            f"(max wait: {max_wait_hours}hrs, polling every "
            f"{POLL_INTERVAL//60}min)"
        )

        while elapsed < max_seconds:
            reply = self.check_for_replies(
                engagement_id=engagement_id,
                handoff_triggered_at=handoff_triggered_at,
            )

            if reply:
                return reply

            logger.debug(
                f"No reply yet — elapsed: {elapsed/3600:.1f}hrs. "
                f"Next check in {POLL_INTERVAL//60} min."
            )
            time.sleep(POLL_INTERVAL)
            elapsed += POLL_INTERVAL

        # Timed out
        logger.warning(
            f"No human reply within {max_wait_hours}hrs "
            f"for engagement {engagement_id}"
        )
        with get_db() as db:
            engagement = db.query(Engagement).filter_by(
                id=engagement_id
            ).first()
            if engagement:
                engagement.status = EngagementStatus.SCORING
                engagement.minutes_to_first_human_response = None

        return None

    # -------------------------------------------------------------------------
    # PARSING
    # -------------------------------------------------------------------------

    def _parse_imap_message(self, parsed_email) -> Optional[dict]:
        try:
            from_address = parsed_email.get("From", "")
            subject      = parsed_email.get("Subject", "")
            date_str     = parsed_email.get("Date", "")

            try:
                received_at = parsedate_to_datetime(date_str)
                if received_at.tzinfo is None:
                    received_at = received_at.replace(tzinfo=timezone.utc)
            except Exception:
                received_at = datetime.now(timezone.utc)

            body = self._extract_body(parsed_email)
            if not body:
                return None

            return {
                "from_address": from_address,
                "subject":      subject,
                "body":         body,
                "received_at":  received_at,
            }

        except Exception as e:
            logger.warning(f"Failed to parse email: {e}")
            return None

    def _extract_body(self, parsed_email) -> str:
        """Extract plain text body from parsed email."""
        if parsed_email.is_multipart():
            for part in parsed_email.walk():
                if part.get_content_type() == "text/plain":
                    try:
                        return part.get_payload(decode=True).decode(
                            "utf-8", errors="replace"
                        )
                    except Exception:
                        continue
        else:
            if parsed_email.get_content_type() == "text/plain":
                try:
                    return parsed_email.get_payload(decode=True).decode(
                        "utf-8", errors="replace"
                    )
                except Exception:
                    pass
        return ""

    # -------------------------------------------------------------------------
    # HEURISTICS
    # -------------------------------------------------------------------------

    def _is_auto_responder(self, reply: dict) -> bool:
        from_addr = reply.get("from_address", "").lower()
        subject   = reply.get("subject", "").lower()
        body      = reply.get("body", "").lower()
        for signal in AUTO_RESPONDER_SIGNALS:
            if signal in from_addr or signal in subject or signal in body[:200]:
                return True
        return False

    def _detect_context_continuity(self, reply_body: str) -> bool:
        body_lower      = reply_body.lower()
        context_signals = [
            self.persona.get("unit_preference", "").lower(),
            self.persona.get("special_needs",   "").lower(),
            "work from home", "relocat", "pet", "dog",
            "move-in special", "concession",
            "timeline", "60 day", "45 day",
        ]
        matches = sum(
            1 for signal in context_signals
            if signal and signal in body_lower
        )
        return matches >= 2

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
