"""
Pipeline Runner - The main entry point that wires everything together.

Flow for each property engagement:
1. Load property and persona from DB / config
2. Launch browser agent → run webchat conversation
3. Trigger email follow-up
4. Monitor inbox for human reply (async, scheduled)
5. Score the completed engagement via Gemini
6. Generate property report

Can be run:
- As a one-off: python -m agent.pipeline --property-id <id>
- As a Cloud Run job triggered by Cloud Scheduler
- Partially: just the webchat phase, just scoring, just reporting
"""

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from loguru import logger

from config.settings import settings
from database.models import (
    Property, Engagement, Message, Score,
    EngagementStatus, MessageSender, ConversationStage, ChannelType
)
from database.connection import get_db, init_db
from agent.orchestrator import Orchestrator
from agent.browser_agent import BrowserAgent
from agent.email_monitor import EmailMonitor
from reports.generator import ReportGenerator


# --- Logging Setup ---

def setup_logging(level: str = "INFO"):
    logger.remove()
    logger.add(
        sys.stdout,
        format="<green>{time:HH:mm:ss}</green> | <level>{level:<8}</level> | {message}",
        level=level,
        colorize=True,
    )
    logger.add(
        "logs/pipeline_{time:YYYY-MM-DD}.log",
        format="{time:YYYY-MM-DD HH:mm:ss} | {level} | {message}",
        level="DEBUG",
        rotation="1 day",
        retention="30 days",
    )


# --- Persona Loader ---

def load_persona(persona_id: str) -> dict:
    """Load a persona from the personas directory."""
    persona_path = Path(f"personas/{persona_id}.json")
    if not persona_path.exists():
        raise FileNotFoundError(f"Persona file not found: {persona_path}")
    with open(persona_path) as f:
        return json.load(f)


# --- Pipeline Phases ---

async def phase_webchat(
    engagement_id: str,
    property_url: str,
    persona: dict,
    orchestrator: Orchestrator,
    headless: bool = True,
) -> dict:
    """
    Phase 1: Run the webchat conversation through all stages.
    Returns the engagement result dict from the browser agent.
    """
    logger.info(f"=== PHASE 1: WEBCHAT | Engagement {engagement_id} ===")

    browser = BrowserAgent(headless=headless, slow_mo=150)

    result = await browser.run_engagement(
        engagement_id=engagement_id,
        property_url=property_url,
        persona=persona,
        orchestrator=orchestrator,
    )

    if result["success"]:
        logger.success(
            f"Webchat complete | Platform: {result['platform']} | "
            f"Messages: {len(result['transcript'])}"
        )
    else:
        logger.error(f"Webchat failed: {result.get('error')}")

    return result


def phase_email(
    engagement_id: str,
    persona: dict,
    property_name: str,
    leasing_email: str,
    webchat_summary: str,
    credentials_path: str,
) -> Optional[str]:
    """
    Phase 2: Send follow-up email from persona inbox.
    Returns Gmail message ID if sent, None if failed.
    """
    logger.info(f"=== PHASE 2: EMAIL FOLLOW-UP | Engagement {engagement_id} ===")

    monitor = EmailMonitor(
        persona=persona,
        credentials_path=credentials_path,
    )
    monitor.authenticate()

    message_id = monitor.send_followup_email(
        engagement_id=engagement_id,
        to_address=leasing_email,
        property_name=property_name,
        webchat_summary=webchat_summary,
    )

    if message_id:
        logger.success(f"Follow-up email sent | Gmail ID: {message_id}")
    else:
        logger.error("Failed to send follow-up email")

    return message_id


def phase_monitor(
    engagement_id: str,
    persona: dict,
    sent_message_id: str,
    handoff_triggered_at: datetime,
    credentials_path: str,
) -> Optional[dict]:
    """
    Phase 3: Monitor inbox for human reply.
    This is the long-running phase — blocks until reply or 72hr timeout.
    In production this runs as a separate Cloud Scheduler job.
    """
    logger.info(f"=== PHASE 3: MONITORING INBOX | Engagement {engagement_id} ===")

    monitor = EmailMonitor(
        persona=persona,
        credentials_path=credentials_path,
    )
    monitor.authenticate()

    reply = monitor.wait_for_reply(
        engagement_id=engagement_id,
        sent_message_id=sent_message_id,
        handoff_triggered_at=handoff_triggered_at,
        max_wait_hours=settings.handoff_wait_hours,
    )

    if reply:
        logger.success(
            f"Human reply received | "
            f"From: {reply.get('from_address')} | "
            f"Subject: {reply.get('subject', '')[:50]}"
        )
    else:
        logger.warning("No human reply received within wait window")

    return reply


def phase_score(
    engagement_id: str,
    orchestrator: Orchestrator,
) -> Optional[dict]:
    """
    Phase 4: Pull full transcript from DB and score with Gemini.
    Saves scores back to DB.
    """
    logger.info(f"=== PHASE 4: SCORING | Engagement {engagement_id} ===")

    with get_db() as db:
        engagement = db.query(Engagement).filter_by(id=engagement_id).first()
        if not engagement:
            logger.error(f"Engagement {engagement_id} not found")
            return None

        messages = db.query(Message).filter_by(
            engagement_id=engagement_id
        ).order_by(Message.sent_at).all()

        transcript = [
            {
                "sender": m.sender.value,
                "channel": m.channel.value,
                "stage": m.stage.value,
                "content": m.content,
                "sent_at": m.sent_at.isoformat() if m.sent_at else "",
            }
            for m in messages
        ]

        minutes_to_human = engagement.minutes_to_first_human_response
        human_had_context = engagement.human_had_context

    if not transcript:
        logger.error(f"No messages found for engagement {engagement_id}")
        return None

    scores = orchestrator.score_engagement(
        engagement_id=engagement_id,
        transcript=transcript,
        minutes_to_human_response=minutes_to_human,
        human_had_context=human_had_context,
    )

    orchestrator.save_scores(engagement_id=engagement_id, scores=scores)

    # Mark engagement complete
    with get_db() as db:
        engagement = db.query(Engagement).filter_by(id=engagement_id).first()
        if engagement:
            engagement.status = EngagementStatus.COMPLETE
            engagement.completed_at = datetime.now(timezone.utc)

    logger.success(f"Scoring complete for engagement {engagement_id}")
    return scores


def phase_report(
    property_id: str,
    orchestrator: Orchestrator,
) -> Optional[Path]:
    """
    Phase 5: Generate the property-level HTML report.
    """
    logger.info(f"=== PHASE 5: REPORT | Property {property_id} ===")

    generator = ReportGenerator()
    report_path = generator.generate_property_report(
        property_id=property_id,
        orchestrator=orchestrator,
        include_transcript=True,
    )

    if report_path:
        logger.success(f"Report written to: {report_path}")
    else:
        logger.error("Report generation failed")

    return report_path


# --- Full Pipeline ---

async def run_full_pipeline(
    property_id: str,
    persona_id: str,
    credentials_path: str,
    headless: bool = True,
    skip_email: bool = False,
    skip_monitor: bool = False,
) -> dict:
    """
    Runs the complete audit pipeline for one property + persona combination.

    Returns a summary dict with paths, IDs, and status.
    """
    summary = {
        "property_id": property_id,
        "persona_id": persona_id,
        "engagement_id": None,
        "webchat_success": False,
        "email_sent": False,
        "human_replied": False,
        "scored": False,
        "report_path": None,
        "errors": [],
    }

    # --- Setup ---
    os.makedirs("logs", exist_ok=True)

    orchestrator = Orchestrator()
    persona = load_persona(persona_id)

    with get_db() as db:
        prop = db.query(Property).filter_by(id=property_id).first()
        if not prop:
            logger.error(f"Property {property_id} not found in database")
            summary["errors"].append("Property not found")
            return summary

        property_name = prop.name
        property_url = prop.website_url
        leasing_email = prop.notes  # Storing contact email in notes for MVP

        # Create engagement record
        engagement = Engagement(
            property_id=property_id,
            persona_id=persona_id,
            status=EngagementStatus.PENDING,
        )
        db.add(engagement)
        db.flush()
        engagement_id = engagement.id

    summary["engagement_id"] = engagement_id
    logger.info(
        f"Starting pipeline | Property: {property_name} | "
        f"Persona: {persona_id} | Engagement: {engagement_id}"
    )

    # --- Phase 1: Webchat ---
    try:
        webchat_result = await phase_webchat(
            engagement_id=engagement_id,
            property_url=property_url,
            persona=persona,
            orchestrator=orchestrator,
            headless=headless,
        )
        summary["webchat_success"] = webchat_result["success"]
        if not webchat_result["success"]:
            summary["errors"].append(f"Webchat: {webchat_result.get('error')}")

    except Exception as e:
        logger.error(f"Phase 1 exception: {e}")
        summary["errors"].append(f"Webchat exception: {str(e)}")

    # --- Phase 2: Email ---
    if not skip_email and summary["webchat_success"] and leasing_email:
        try:
            webchat_summary = (
                f"Persona asked about {persona['unit_preference']} availability, "
                f"pricing, and {persona['special_needs']}. "
                f"Handoff was triggered after {len(webchat_result['transcript'])} messages."
            )

            message_id = phase_email(
                engagement_id=engagement_id,
                persona=persona,
                property_name=property_name,
                leasing_email=leasing_email,
                webchat_summary=webchat_summary,
                credentials_path=credentials_path,
            )
            summary["email_sent"] = message_id is not None

        except Exception as e:
            logger.error(f"Phase 2 exception: {e}")
            summary["errors"].append(f"Email exception: {str(e)}")
    else:
        logger.info("Skipping email phase")

    # --- Phase 3: Monitor ---
    if not skip_monitor and summary["email_sent"]:
        try:
            with get_db() as db:
                engagement = db.query(Engagement).filter_by(
                    id=engagement_id
                ).first()
                handoff_at = engagement.handoff_triggered_at or datetime.now(timezone.utc)

            reply = phase_monitor(
                engagement_id=engagement_id,
                persona=persona,
                sent_message_id=message_id,
                handoff_triggered_at=handoff_at,
                credentials_path=credentials_path,
            )
            summary["human_replied"] = reply is not None

        except Exception as e:
            logger.error(f"Phase 3 exception: {e}")
            summary["errors"].append(f"Monitor exception: {str(e)}")
    else:
        logger.info("Skipping monitor phase")

    # --- Phase 4: Score ---
    try:
        scores = phase_score(
            engagement_id=engagement_id,
            orchestrator=orchestrator,
        )
        summary["scored"] = scores is not None

    except Exception as e:
        logger.error(f"Phase 4 exception: {e}")
        summary["errors"].append(f"Scoring exception: {str(e)}")

    # --- Phase 5: Report ---
    try:
        report_path = phase_report(
            property_id=property_id,
            orchestrator=orchestrator,
        )
        if report_path:
            summary["report_path"] = str(report_path)

    except Exception as e:
        logger.error(f"Phase 5 exception: {e}")
        summary["errors"].append(f"Report exception: {str(e)}")

    # --- Final Summary ---
    logger.info("=" * 60)
    logger.info("PIPELINE COMPLETE")
    logger.info(f"  Engagement ID : {engagement_id}")
    logger.info(f"  Webchat       : {'✓' if summary['webchat_success'] else '✗'}")
    logger.info(f"  Email sent    : {'✓' if summary['email_sent'] else '✗'}")
    logger.info(f"  Human replied : {'✓' if summary['human_replied'] else '✗'}")
    logger.info(f"  Scored        : {'✓' if summary['scored'] else '✗'}")
    logger.info(f"  Report        : {summary['report_path'] or '✗'}")
    if summary["errors"]:
        logger.warning(f"  Errors        : {summary['errors']}")
    logger.info("=" * 60)

    return summary


# --- CLI Entry Point ---

def main():
    parser = argparse.ArgumentParser(
        description="Leasing AI Auditor — Pipeline Runner"
    )

    subparsers = parser.add_subparsers(dest="command")

    # --- init-db command ---
    subparsers.add_parser("init-db", help="Initialize database tables")

    # --- add-property command ---
    add_prop = subparsers.add_parser("add-property", help="Add a property to audit")
    add_prop.add_argument("--name", required=True)
    add_prop.add_argument("--url", required=True)
    add_prop.add_argument("--email", required=True, help="Leasing contact email")
    add_prop.add_argument("--company", default=None)
    add_prop.add_argument("--market", default=None)

    # --- run command ---
    run_cmd = subparsers.add_parser("run", help="Run a full pipeline engagement")
    run_cmd.add_argument("--property-id", required=True)
    run_cmd.add_argument("--persona", default="maya", choices=["maya", "garcia"])
    run_cmd.add_argument("--credentials", default="credentials.json")
    run_cmd.add_argument("--no-headless", action="store_true",
                         help="Show browser window (local dev only)")
    run_cmd.add_argument("--skip-email", action="store_true")
    run_cmd.add_argument("--skip-monitor", action="store_true")

    # --- score command (re-score an existing engagement) ---
    score_cmd = subparsers.add_parser("score", help="Score an existing engagement")
    score_cmd.add_argument("--engagement-id", required=True)

    # --- report command ---
    report_cmd = subparsers.add_parser("report", help="Generate property report")
    report_cmd.add_argument("--property-id", required=True)

    # --- list command ---
    subparsers.add_parser("list", help="List all properties in the database")

    args = parser.parse_args()

    setup_logging()

    if args.command == "init-db":
        init_db()

    elif args.command == "add-property":
        with get_db() as db:
            prop = Property(
                name=args.name,
                website_url=args.url,
                management_company=args.company,
                market=args.market,
                notes=args.email,  # Leasing email stored in notes for MVP
            )
            db.add(prop)
            db.flush()
            print(f"Property added: {prop.name} | ID: {prop.id}")

    elif args.command == "run":
        result = asyncio.run(run_full_pipeline(
            property_id=args.property_id,
            persona_id=args.persona,
            credentials_path=args.credentials,
            headless=not args.no_headless,
            skip_email=args.skip_email,
            skip_monitor=args.skip_monitor,
        ))
        sys.exit(0 if not result["errors"] else 1)

    elif args.command == "score":
        orchestrator = Orchestrator()
        scores = phase_score(
            engagement_id=args.engagement_id,
            orchestrator=orchestrator,
        )
        if scores:
            print(json.dumps(scores, indent=2))

    elif args.command == "report":
        orchestrator = Orchestrator()
        report_path = phase_report(
            property_id=args.property_id,
            orchestrator=orchestrator,
        )
        if report_path:
            print(f"Report: {report_path}")

    elif args.command == "list":
        with get_db() as db:
            properties = db.query(Property).filter_by(active=True).all()
            if not properties:
                print("No properties in database. Use add-property to add one.")
            for p in properties:
                engagements = db.query(Engagement).filter_by(
                    property_id=p.id
                ).count()
                print(f"  {p.id[:8]}... | {p.name} | {p.market or 'N/A'} | {engagements} engagements")

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
