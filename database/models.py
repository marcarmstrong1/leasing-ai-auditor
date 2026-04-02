from sqlalchemy import (
    Column, String, Integer, Float, DateTime, Text,
    ForeignKey, Enum, Boolean
)
from sqlalchemy.orm import declarative_base, relationship
from sqlalchemy.sql import func
import enum
import uuid

Base = declarative_base()

def generate_uuid():
    return str(uuid.uuid4())

# --- Enums ---

class EngagementStatus(str, enum.Enum):
    PENDING = "pending"           # Created, not started
    IN_PROGRESS = "in_progress"   # Browser agent active
    AWAITING_HUMAN = "awaiting_human"  # Handoff triggered, waiting
    SCORING = "scoring"           # Human responded, scoring in progress
    COMPLETE = "complete"         # Fully scored
    FAILED = "failed"             # Something broke

class MessageSender(str, enum.Enum):
    PERSONA = "persona"           # Our agent sent this
    AI_BOT = "ai_bot"             # Property chatbot responded
    HUMAN_LEASING = "human_leasing"  # Human leasing agent responded
    UNKNOWN = "unknown"           # Can't determine

class ConversationStage(str, enum.Enum):
    DISCOVERY = "discovery"
    NUANCE = "nuance"
    HANDOFF_TRIGGER = "handoff_trigger"
    HUMAN_FOLLOWUP = "human_followup"

class ChannelType(str, enum.Enum):
    WEBCHAT = "webchat"
    EMAIL = "email"

# --- Models ---

class Property(Base):
    """
    A multifamily property being audited.
    """
    __tablename__ = "properties"

    id = Column(String, primary_key=True, default=generate_uuid)
    name = Column(String, nullable=False)
    website_url = Column(String, nullable=False)
    chatbot_url = Column(String, nullable=True)  # If different from main site
    management_company = Column(String, nullable=True)
    market = Column(String, nullable=True)        # e.g. "Houston", "Dallas"
    property_class = Column(String, nullable=True) # A, B, C
    unit_count = Column(Integer, nullable=True)
    notes = Column(Text, nullable=True)
    active = Column(Boolean, default=True)
    created_at = Column(DateTime, server_default=func.now())

    engagements = relationship("Engagement", back_populates="property")

    def __repr__(self):
        return f"<Property {self.name}>"


class Engagement(Base):
    """
    A single end-to-end audit run for one property using one persona.
    Tracks the full lifecycle from first chatbot message to human follow-up scoring.
    """
    __tablename__ = "engagements"

    id = Column(String, primary_key=True, default=generate_uuid)
    property_id = Column(String, ForeignKey("properties.id"), nullable=False)
    persona_id = Column(String, nullable=False)   # "maya", "garcia", etc.
    status = Column(Enum(EngagementStatus), default=EngagementStatus.PENDING)

    # Timing
    started_at = Column(DateTime, nullable=True)
    handoff_triggered_at = Column(DateTime, nullable=True)
    first_human_response_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)

    # Computed timing metrics (stored for fast reporting)
    minutes_to_first_human_response = Column(Float, nullable=True)

    # Chatbot platform detected during engagement
    chatbot_platform = Column(String, nullable=True)  # e.g. "EliseAI", "Funnel", "Knock"

    # Did the human response reference the prior AI conversation?
    human_had_context = Column(Boolean, nullable=True)

    # Raw notes from the orchestrator
    orchestrator_notes = Column(Text, nullable=True)

    created_at = Column(DateTime, server_default=func.now())

    property = relationship("Property", back_populates="engagements")
    messages = relationship("Message", back_populates="engagement",
                            order_by="Message.sent_at")
    scores = relationship("Score", back_populates="engagement")

    def __repr__(self):
        return f"<Engagement {self.id} | {self.persona_id} @ {self.property_id}>"


class Message(Base):
    """
    A single message in an engagement conversation.
    Covers both webchat and email channels.
    """
    __tablename__ = "messages"

    id = Column(String, primary_key=True, default=generate_uuid)
    engagement_id = Column(String, ForeignKey("engagements.id"), nullable=False)

    sender = Column(Enum(MessageSender), nullable=False)
    channel = Column(Enum(ChannelType), nullable=False)
    stage = Column(Enum(ConversationStage), nullable=False)

    content = Column(Text, nullable=False)
    sent_at = Column(DateTime, nullable=False)

    # For email messages
    email_subject = Column(String, nullable=True)
    email_thread_id = Column(String, nullable=True)

    # Orchestrator's real-time assessment of this specific message
    # (separate from the final rubric score)
    immediate_notes = Column(Text, nullable=True)

    engagement = relationship("Engagement", back_populates="messages")

    def __repr__(self):
        return f"<Message {self.sender} | {self.channel} | {self.stage}>"


class Score(Base):
    """
    Rubric scores for a completed engagement.
    One row per scoring dimension per engagement.
    """
    __tablename__ = "scores"

    id = Column(String, primary_key=True, default=generate_uuid)
    engagement_id = Column(String, ForeignKey("engagements.id"), nullable=False)

    # Scoring dimensions
    dimension = Column(String, nullable=False)  # matches rubric keys
    score = Column(Float, nullable=False)        # 1.0 - 5.0
    max_score = Column(Float, default=5.0)
    rationale = Column(Text, nullable=True)      # Gemini's explanation

    scored_at = Column(DateTime, server_default=func.now())

    engagement = relationship("Engagement", back_populates="scores")

    def __repr__(self):
        return f"<Score {self.dimension}: {self.score}/5>"


class PropertyReport(Base):
    """
    Aggregated report for a property across all engagements.
    Regenerated whenever new engagements complete.
    """
    __tablename__ = "property_reports"

    id = Column(String, primary_key=True, default=generate_uuid)
    property_id = Column(String, ForeignKey("properties.id"), nullable=False)

    # Aggregate scores (averages across engagements)
    score_ai_responsiveness = Column(Float, nullable=True)
    score_ai_accuracy = Column(Float, nullable=True)
    score_handoff_communication = Column(Float, nullable=True)
    score_context_continuity = Column(Float, nullable=True)
    score_human_response_speed = Column(Float, nullable=True)
    score_human_quality = Column(Float, nullable=True)

    # Signature metric
    score_handoff_index = Column(Float, nullable=True)  # avg of dims 3, 4, 5

    # Overall
    score_overall = Column(Float, nullable=True)

    # Gemini-generated narrative summary
    narrative_summary = Column(Text, nullable=True)

    # How many engagements this is based on
    engagement_count = Column(Integer, default=0)

    generated_at = Column(DateTime, server_default=func.now())

    def __repr__(self):
        return f"<PropertyReport {self.property_id} | Overall: {self.score_overall}>"
