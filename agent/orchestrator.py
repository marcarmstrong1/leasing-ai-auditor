"""
Orchestrator - The Gemini brain of the leasing AI auditor.

Two core jobs:
1. PERSONA ENGINE - Given conversation history and current stage,
   generate the next message as the renter persona.
2. SCORER - Given a complete or in-progress engagement transcript,
   evaluate responses against the rubric and return structured scores.
"""

import json
import re
from datetime import datetime
from typing import Optional
from loguru import logger

import vertexai
from vertexai.generative_models import GenerativeModel, GenerationConfig

from config.settings import settings
from database.models import (
    Engagement, Message, Score,
    MessageSender, ConversationStage, ChannelType
)
from database.connection import get_db


# --- Rubric Definition ---

SCORING_RUBRIC = {
    "ai_responsiveness": {
        "description": "Speed and completeness of chatbot answers during discovery and nuance stages",
        "weight": 1.0,
        "criteria": {
            5: "Responded instantly or near-instantly. Answered all questions completely without prompting for clarification unnecessarily.",
            4: "Responded quickly. Answered most questions completely with minor gaps.",
            3: "Responded adequately but missed some questions or required follow-up to get complete answers.",
            2: "Slow to respond or frequently incomplete. Required significant follow-up.",
            1: "Failed to respond meaningfully or left major questions unanswered."
        }
    },
    "ai_accuracy": {
        "description": "Factual correctness of chatbot responses - pricing, availability, policies",
        "weight": 1.0,
        "criteria": {
            5: "All information provided was accurate and consistent. No contradictions detected.",
            4: "Mostly accurate with one minor inconsistency or uncertainty.",
            3: "Some accuracy issues - contradicted itself or gave vague non-answers to factual questions.",
            2: "Notable accuracy problems. Gave conflicting information or clearly wrong details.",
            1: "Significantly inaccurate or refused to provide basic factual information."
        }
    },
    "handoff_communication": {
        "description": "How clearly the AI communicated the transition to a human agent",
        "weight": 1.5,
        "criteria": {
            5: "Explicitly acknowledged handoff, set clear timeline expectations, confirmed contact info, and expressed warmth.",
            4: "Acknowledged handoff and collected contact info but timeline expectations were vague.",
            3: "Indicated a human would follow up but gave no timeline or confirmation.",
            2: "Vague handoff - unclear if a human would actually be involved or when.",
            1: "No handoff communication. Conversation ended or looped without escalation."
        }
    },
    "context_continuity": {
        "description": "Whether the human leasing agent had context from the prior AI conversation",
        "weight": 1.5,
        "criteria": {
            5: "Human referenced specific details from the chatbot conversation unprompted. Clear context transfer.",
            4: "Human seemed generally aware of prior conversation but did not reference specifics.",
            3: "Human had partial context - knew to follow up but asked prospect to repeat some information.",
            2: "Human had minimal context. Prospect had to re-explain most of their situation.",
            1: "No context transfer. Human started completely from scratch with no reference to prior conversation."
        }
    },
    "human_response_speed": {
        "description": "Time from handoff trigger to first human response, benchmarked against AI speed",
        "weight": 1.5,
        "criteria": {
            5: "Human responded within 1 hour.",
            4: "Human responded within 4 hours.",
            3: "Human responded within 24 hours.",
            2: "Human responded within 72 hours.",
            1: "Human did not respond within 72 hours or did not respond at all."
        }
    },
    "human_quality": {
        "description": "Warmth, relevance, and completeness of the human leasing agent response",
        "weight": 1.0,
        "criteria": {
            5: "Response was warm, personalized, addressed the specific questions asked, and moved the conversation forward proactively.",
            4: "Response was professional and relevant. Addressed most questions with minor gaps.",
            3: "Response was adequate but generic. Did not feel personalized to the prospect's situation.",
            2: "Response was perfunctory or missed the point of the prospect's questions.",
            1: "Response was unhelpful, robotic, or failed to address what was asked."
        }
    }
}


# --- Orchestrator Class ---

class Orchestrator:
    def __init__(self):
        vertexai.init(
            project=settings.project_id,
            location=settings.region
        )
        self.model = GenerativeModel(
            model_name="gemini-2.5-pro",
            generation_config=GenerationConfig(
                temperature=settings.gemini_temperature,
                max_output_tokens=settings.gemini_max_tokens,
            )
        )
        logger.info("Orchestrator initialized with Gemini 2.5 Pro")

    # -------------------------------------------------------------------------
    # PERSONA ENGINE
    # -------------------------------------------------------------------------

    def generate_persona_message(
        self,
        persona: dict,
        stage: ConversationStage,
        conversation_history: list[dict],
        last_property_message: str,
        channel: ChannelType = ChannelType.WEBCHAT
    ) -> str:
        """
        Given the current conversation state, generate the next message
        the persona would naturally send.

        Returns a plain string — the message text to send.
        """

        history_text = self._format_history(conversation_history)
        stage_guidance = self._get_stage_guidance(stage, persona)
        channel_guidance = "Keep the message concise and conversational, like a real chat message." \
            if channel == ChannelType.WEBCHAT else \
            "Write in a natural email tone — slightly more formal than chat but still warm and human."

        prompt = f"""
You are roleplaying as a prospective apartment renter with the following profile:

NAME: {persona['name']}
BACKGROUND: {persona['background']}
TIMELINE: {persona['timeline']}
BUDGET: ${persona['budget_min']} - ${persona['budget_max']}/month
UNIT PREFERENCE: {persona['unit_preference']}
SPECIAL NEEDS: {persona['special_needs']}
HAS PET: {persona.get('pet', False)} {f"({persona.get('pet_details', '')})" if persona.get('pet') else ""}

CONVERSATION SO FAR:
{history_text}

THE PROPERTY JUST SAID:
{last_property_message}

CURRENT STAGE: {stage.value}
STAGE GUIDANCE: {stage_guidance}

CHANNEL: {channel.value}
{channel_guidance}

YOUR TASK:
Write the next message {persona['name']} would naturally send. 

CRITICAL RULES:
- Sound like a real human renter, NOT like an AI or a survey
- Do NOT ask more than 2 questions at once
- React naturally to what the property just said before moving to your next point
- Stay consistent with the persona's background and needs
- At the HANDOFF_TRIGGER stage, clearly but naturally request to speak with a human
- Do NOT reveal you are testing the system under any circumstances
- Keep webchat messages under 100 words

Respond with ONLY the message text. No labels, no explanation, just the message.
""".strip()

        response = self.model.generate_content(prompt)
        message = response.text.strip()
        logger.debug(f"Persona message generated for stage {stage.value}: {message[:80]}...")
        return message

    def _get_stage_guidance(self, stage: ConversationStage, persona: dict) -> str:
        guidance = {
            ConversationStage.DISCOVERY: (
                "Ask about basic availability and pricing. Be friendly and curious. "
                "You're just getting oriented — don't overwhelm them with questions."
            ),
            ConversationStage.NUANCE: (
                "Dig into specifics that require real judgment — not just lookups. "
                f"Focus on: {persona['special_needs']}. "
                "Ask something that might trip up a simple chatbot."
            ),
            ConversationStage.HANDOFF_TRIGGER: (
                "Naturally express that you have more specific questions and would "
                "feel more comfortable talking to a person before scheduling a tour. "
                "Ask if a leasing agent can reach out. "
                f"If providing an email, use the persona's real email: {persona['email']}. "
                "Do not make up or use placeholder email addresses."
            ),
            ConversationStage.HUMAN_FOLLOWUP: (
                "Respond to whatever the human leasing agent said. "
                "Be warm and engaged. Ask one clarifying follow-up if appropriate."
            ),
        }
        return guidance.get(stage, "Continue the conversation naturally.")

    def _format_history(self, history: list[dict]) -> str:
        if not history:
            return "(No messages yet — this is the opening message)"
        lines = []
        for msg in history:
            sender_label = "YOU (persona)" if msg["sender"] == "persona" else "PROPERTY"
            lines.append(f"{sender_label}: {msg['content']}")
        return "\n".join(lines)

    # -------------------------------------------------------------------------
    # SCORER
    # -------------------------------------------------------------------------

    def score_engagement(
        self,
        engagement_id: str,
        transcript: list[dict],
        minutes_to_human_response: Optional[float] = None,
        human_had_context: Optional[bool] = None
    ) -> dict:
        """
        Score a completed or in-progress engagement against the full rubric.

        Returns a dict of {dimension: {score, rationale}} for all dimensions
        that can be evaluated given the current transcript state.
        """

        transcript_text = self._format_transcript_for_scoring(transcript)
        rubric_text = self._format_rubric_for_prompt()

        # Build context about what we know
        timing_context = ""
        if minutes_to_human_response is not None:
            hours = minutes_to_human_response / 60
            timing_context = f"\nHUMAN RESPONSE TIME: {hours:.1f} hours ({minutes_to_human_response:.0f} minutes)"
        if human_had_context is not None:
            timing_context += f"\nHUMAN HAD CONVERSATION CONTEXT: {human_had_context}"

        prompt = f"""
You are a senior researcher at J Turner Research evaluating a multifamily leasing communication audit.

A trained evaluator posed as a prospective renter and engaged with an apartment community's chatbot and leasing team. You will score the engagement against a research rubric.

FULL CONVERSATION TRANSCRIPT:
{transcript_text}
{timing_context}

SCORING RUBRIC:
{rubric_text}

YOUR TASK:
Score each dimension on a 1-5 scale based strictly on the evidence in the transcript.
If a dimension cannot yet be evaluated (e.g., no human response has occurred yet),
set the score to null and explain why in the rationale.

Respond ONLY with a valid JSON object in exactly this format:
{{
  "ai_responsiveness": {{"score": <1-5 or null>, "rationale": "<1-2 sentences>"}},
  "ai_accuracy": {{"score": <1-5 or null>, "rationale": "<1-2 sentences>"}},
  "handoff_communication": {{"score": <1-5 or null>, "rationale": "<1-2 sentences>"}},
  "context_continuity": {{"score": <1-5 or null>, "rationale": "<1-2 sentences>"}},
  "human_response_speed": {{"score": <1-5 or null>, "rationale": "<1-2 sentences>"}},
  "human_quality": {{"score": <1-5 or null>, "rationale": "<1-2 sentences>"}},
  "overall_notes": "<2-3 sentences summarizing the most important findings>"
}}

No markdown, no explanation outside the JSON. Just the JSON object.
""".strip()

        response = self.model.generate_content(prompt)
        raw = response.text.strip()

        # Strip markdown fences if Gemini adds them
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        raw = raw.strip()

        try:
            scores = json.loads(raw)
            logger.success(f"Scoring complete for engagement {engagement_id}")
            return scores
        except json.JSONDecodeError as e:
            logger.warning(f"Initial JSON parse failed: {e} — attempting repair")
            try:
                # Fix 1: Remove trailing commas before } or ]
                repaired = re.sub(r",\s*([}\]])", r"\1", raw)
                # Fix 2: Ensure the JSON is terminated properly
                # Count braces and close any that are open
                open_braces = repaired.count("{") - repaired.count("}")
                open_brackets = repaired.count("[") - repaired.count("]")
                repaired += "}" * open_braces + "]" * open_brackets
                scores = json.loads(repaired)
                logger.success(f"Scoring complete after JSON repair for {engagement_id}")
                return scores
            except json.JSONDecodeError:
                # Fix 3: Extract whatever valid dimension blocks we can
                logger.warning("Attempting partial score extraction...")
                partial = {}
                dimensions = [
                    "ai_responsiveness", "ai_accuracy", "handoff_communication",
                    "context_continuity", "human_response_speed", "human_quality"
                ]
                for dim in dimensions:
                    pattern = rf'"{dim}"\s*:\s*{{[^}}]*"score"\s*:\s*(\d+(?:\.\d+)?)[^}}]*"rationale"\s*:\s*"([^"]*)"'
                    match = re.search(pattern, raw, re.DOTALL)
                    if match:
                        partial[dim] = {
                            "score": float(match.group(1)),
                            "rationale": match.group(2)
                        }
                # Extract overall notes
                notes_match = re.search(r'"overall_notes"\s*:\s*"([^"]*)"', raw)
                if notes_match:
                    partial["overall_notes"] = notes_match.group(1)
                if partial:
                    logger.success(f"Partial scores extracted: {list(partial.keys())}")
                    return partial
                logger.error(f"Could not parse scoring JSON: {e}\nRaw: {raw}")
                raise

    def _format_transcript_for_scoring(self, transcript: list[dict]) -> str:
        lines = []
        for msg in transcript:
            timestamp = msg.get("sent_at", "unknown time")
            sender = msg.get("sender", "unknown")
            channel = msg.get("channel", "webchat")
            stage = msg.get("stage", "unknown")
            content = msg.get("content", "")
            lines.append(
                f"[{timestamp}] [{channel.upper()}] [{stage.upper()}] {sender.upper()}: {content}"
            )
        return "\n\n".join(lines)

    def _format_rubric_for_prompt(self) -> str:
        lines = []
        for dimension, info in SCORING_RUBRIC.items():
            lines.append(f"\n{dimension.upper()} (weight: {info['weight']}x)")
            lines.append(f"  {info['description']}")
            for score_val, criteria in info['criteria'].items():
                lines.append(f"  {score_val}: {criteria}")
        return "\n".join(lines)

    # -------------------------------------------------------------------------
    # SAVE SCORES TO DB
    # -------------------------------------------------------------------------

    def save_scores(self, engagement_id: str, scores: dict) -> None:
        """
        Persists Gemini's scoring output to the scores table.
        """
        overall_notes = scores.pop("overall_notes", None)

        with get_db() as db:
            for dimension, result in scores.items():
                if dimension == "overall_notes":
                    continue
                score_val = result.get("score")
                if score_val is None:
                    continue
                record = Score(
                    engagement_id=engagement_id,
                    dimension=dimension,
                    score=float(score_val),
                    rationale=result.get("rationale", ""),
                    scored_at=datetime.utcnow()
                )
                db.add(record)

            # Save overall notes back to engagement
            if overall_notes:
                engagement = db.query(Engagement).filter_by(id=engagement_id).first()
                if engagement:
                    engagement.orchestrator_notes = overall_notes

        logger.info(f"Scores saved for engagement {engagement_id}")

    # -------------------------------------------------------------------------
    # NARRATIVE SUMMARY (for reports)
    # -------------------------------------------------------------------------

    def generate_property_narrative(
        self,
        property_name: str,
        scores: dict,
        engagement_notes: list[str]
    ) -> str:
        """
        Generates a 2-3 paragraph human-readable narrative for the property report.
        """

        scores_text = "\n".join([
            f"- {dim}: {val}/5" for dim, val in scores.items() if val is not None
        ])
        notes_text = "\n".join(engagement_notes) if engagement_notes else "No additional notes."

        prompt = f"""
You are a research analyst at J Turner Research writing a property communication audit report.

PROPERTY: {property_name}

SCORES:
{scores_text}

ENGAGEMENT NOTES:
{notes_text}

Write a 2-3 paragraph narrative summary of this property's leasing communication performance.
Focus on: what they did well, where the breakdown occurred, and the most important thing
they could do to improve. Write in a professional but direct research tone.
Do not use bullet points. Do not repeat the raw scores — interpret them.
""".strip()

        response = self.model.generate_content(prompt)
        return response.text.strip()
