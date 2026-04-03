"""
Report Generator - Property-level audit report builder.

Takes completed engagement data from the database and produces:
1. A structured property scorecard (JSON)
2. A human-readable HTML report
3. A narrative summary via Gemini

Designed to be run after all engagements for a property are complete,
or on-demand for partial results.
"""

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from loguru import logger
from jinja2 import Environment, BaseLoader

from database.models import (
    Property, Engagement, Message, Score, PropertyReport,
    EngagementStatus, MessageSender
)
from database.connection import get_db


# --- Scoring thresholds for grade labels ---

GRADE_THRESHOLDS = {
    (4.5, 5.0): ("A+", "#1a7a4a"),
    (4.0, 4.5): ("A",  "#2d9e63"),
    (3.5, 4.0): ("B+", "#5aab4a"),
    (3.0, 3.5): ("B",  "#8cc43a"),
    (2.5, 3.0): ("C+", "#f0a500"),
    (2.0, 2.5): ("C",  "#e07b00"),
    (1.5, 2.0): ("D",  "#d44000"),
    (0.0, 1.5): ("F",  "#b91c1c"),
}

DIMENSION_LABELS = {
    "ai_responsiveness":      "AI Responsiveness",
    "ai_accuracy":            "AI Accuracy",
    "handoff_communication":  "Handoff Communication",
    "context_continuity":     "Context Continuity",
    "human_response_speed":   "Human Response Speed",
    "human_quality":          "Human Quality",
}

# Handoff Index = average of these three dimensions
HANDOFF_INDEX_DIMENSIONS = [
    "handoff_communication",
    "context_continuity",
    "human_response_speed",
]

# --- HTML Report Template ---

REPORT_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Leasing AI Audit — {{ property.name }}</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }

  body {
    font-family: 'Segoe UI', Arial, sans-serif;
    background: #f4f6f9;
    color: #1a1a2e;
    padding: 40px 20px;
  }

  .container { max-width: 900px; margin: 0 auto; }

  /* Header */
  .header {
    background: #1a1a2e;
    color: white;
    padding: 36px 40px;
    border-radius: 12px 12px 0 0;
  }
  .header .brand {
    font-size: 12px;
    letter-spacing: 2px;
    text-transform: uppercase;
    color: #8892a4;
    margin-bottom: 8px;
  }
  .header h1 { font-size: 28px; font-weight: 700; }
  .header .meta {
    margin-top: 10px;
    font-size: 14px;
    color: #8892a4;
  }

  /* Overall Score Banner */
  .score-banner {
    background: white;
    border-left: 6px solid {{ overall_color }};
    padding: 28px 40px;
    display: flex;
    align-items: center;
    gap: 32px;
    box-shadow: 0 2px 8px rgba(0,0,0,0.06);
  }
  .overall-grade {
    font-size: 72px;
    font-weight: 800;
    color: {{ overall_color }};
    line-height: 1;
  }
  .overall-details h2 {
    font-size: 18px;
    color: #1a1a2e;
  }
  .overall-details .score-num {
    font-size: 32px;
    font-weight: 700;
    color: {{ overall_color }};
  }
  .overall-details .engagement-count {
    font-size: 13px;
    color: #8892a4;
    margin-top: 4px;
  }

  /* Handoff Index Callout */
  .handoff-callout {
    background: #1a1a2e;
    color: white;
    padding: 20px 40px;
    display: flex;
    align-items: center;
    justify-content: space-between;
  }
  .handoff-callout .label {
    font-size: 13px;
    text-transform: uppercase;
    letter-spacing: 1px;
    color: #8892a4;
  }
  .handoff-callout .value {
    font-size: 28px;
    font-weight: 700;
    color: {{ handoff_color }};
  }
  .handoff-callout .description {
    font-size: 13px;
    color: #8892a4;
    max-width: 400px;
  }

  /* Dimension Scores */
  .section {
    background: white;
    padding: 32px 40px;
    margin-top: 2px;
    box-shadow: 0 1px 4px rgba(0,0,0,0.04);
  }
  .section h3 {
    font-size: 13px;
    text-transform: uppercase;
    letter-spacing: 1.5px;
    color: #8892a4;
    margin-bottom: 20px;
  }

  .dimension-row {
    display: flex;
    align-items: center;
    margin-bottom: 16px;
    gap: 16px;
  }
  .dimension-label {
    width: 200px;
    font-size: 14px;
    font-weight: 600;
    color: #1a1a2e;
    flex-shrink: 0;
  }
  .score-bar-container {
    flex: 1;
    background: #eef0f3;
    border-radius: 4px;
    height: 10px;
    position: relative;
  }
  .score-bar {
    height: 100%;
    border-radius: 4px;
    background: {{ '{{ color }}' }};
    width: {{ '{{ width }}' }};
    transition: width 0.3s;
  }
  .score-value {
    width: 36px;
    text-align: right;
    font-size: 14px;
    font-weight: 700;
    color: #1a1a2e;
  }
  .score-grade {
    width: 32px;
    text-align: center;
    font-size: 12px;
    font-weight: 700;
    color: {{ '{{ color }}' }};
  }
  .rationale {
    font-size: 12px;
    color: #6b7280;
    margin-top: 4px;
    margin-left: 216px;
    margin-bottom: 12px;
    line-height: 1.5;
  }

  /* Timing Stats */
  .stats-grid {
    display: grid;
    grid-template-columns: repeat(3, 1fr);
    gap: 16px;
    margin-top: 8px;
  }
  .stat-card {
    background: #f8f9fb;
    border-radius: 8px;
    padding: 16px;
    text-align: center;
  }
  .stat-card .stat-value {
    font-size: 28px;
    font-weight: 700;
    color: #1a1a2e;
  }
  .stat-card .stat-label {
    font-size: 12px;
    color: #8892a4;
    margin-top: 4px;
  }

  /* Narrative */
  .narrative {
    font-size: 15px;
    line-height: 1.8;
    color: #374151;
  }
  .narrative p { margin-bottom: 16px; }

  /* Transcript */
  .transcript-entry {
    border-left: 3px solid #eef0f3;
    padding: 12px 16px;
    margin-bottom: 12px;
    font-size: 13px;
    line-height: 1.6;
  }
  .transcript-entry.persona { border-color: #3b82f6; }
  .transcript-entry.ai_bot { border-color: #10b981; }
  .transcript-entry.human_leasing { border-color: #f59e0b; }

  .transcript-meta {
    font-size: 11px;
    color: #9ca3af;
    margin-bottom: 4px;
    text-transform: uppercase;
    letter-spacing: 0.5px;
  }
  .transcript-content { color: #374151; }

  /* Footer */
  .footer {
    background: #1a1a2e;
    color: #8892a4;
    padding: 20px 40px;
    border-radius: 0 0 12px 12px;
    font-size: 12px;
    display: flex;
    justify-content: space-between;
  }
</style>
</head>
<body>
<div class="container">

  <!-- Header -->
  <div class="header">
    <div class="brand">J Turner Research · Leasing AI Audit</div>
    <h1>{{ property.name }}</h1>
    <div class="meta">
      {{ property.management_company or "Management Company N/A" }} ·
      {{ property.market or "Market N/A" }} ·
      Generated {{ generated_at }}
    </div>
  </div>

  <!-- Overall Score -->
  <div class="score-banner">
    <div class="overall-grade">{{ overall_grade }}</div>
    <div class="overall-details">
      <h2>Overall Communication Score</h2>
      <div class="score-num">{{ "%.1f"|format(overall_score) }} / 5.0</div>
      <div class="engagement-count">
        Based on {{ engagement_count }} engagement(s) across
        {{ persona_count }} persona(s)
      </div>
    </div>
  </div>

  <!-- Handoff Index -->
  <div class="handoff-callout">
    <div>
      <div class="label">Handoff Index™</div>
      <div class="value">{{ "%.1f"|format(handoff_index) }} / 5.0</div>
    </div>
    <div class="description">
      J Turner's signature metric measuring the quality of the AI-to-human
      transition: handoff communication, context continuity, and human
      response speed.
    </div>
  </div>

  <!-- Dimension Scores -->
  <div class="section">
    <h3>Scoring Breakdown</h3>
    {% for dim_key, dim_label in dimensions.items() %}
    {% set score = scores.get(dim_key) %}
    {% if score is not none %}
    {% set color = score_color(score) %}
    {% set grade = score_grade(score) %}
    <div class="dimension-row">
      <div class="dimension-label">{{ dim_label }}</div>
      <div class="score-bar-container">
        <div class="score-bar" style="background:{{ color }};width:{{ (score/5*100)|int }}%"></div>
      </div>
      <div class="score-value">{{ "%.1f"|format(score) }}</div>
      <div class="score-grade" style="color:{{ color }}">{{ grade }}</div>
    </div>
    {% if rationales.get(dim_key) %}
    <div class="rationale">{{ rationales[dim_key] }}</div>
    {% endif %}
    {% endif %}
    {% endfor %}
  </div>

  <!-- Timing Stats -->
  <div class="section">
    <h3>Response Timing</h3>
    <div class="stats-grid">
      <div class="stat-card">
        <div class="stat-value">
          {% if avg_human_response_minutes %}
            {{ "%.0f"|format(avg_human_response_minutes / 60) }}h
          {% else %}
            N/A
          {% endif %}
        </div>
        <div class="stat-label">Avg Human Response Time</div>
      </div>
      <div class="stat-card">
        <div class="stat-value">
          {{ context_continuity_rate }}%
        </div>
        <div class="stat-label">Context Transfer Rate</div>
      </div>
      <div class="stat-card">
        <div class="stat-value">{{ engagement_count }}</div>
        <div class="stat-label">Total Engagements</div>
      </div>
    </div>
  </div>

  <!-- Narrative Summary -->
  <div class="section">
    <h3>Research Summary</h3>
    <div class="narrative">
      {% for paragraph in narrative_paragraphs %}
      <p>{{ paragraph }}</p>
      {% endfor %}
    </div>
  </div>

  <!-- Transcript Sample -->
  {% if transcript %}
  <div class="section">
    <h3>Sample Engagement Transcript</h3>
    {% for msg in transcript %}
    <div class="transcript-entry {{ msg.sender }}">
      <div class="transcript-meta">
        {{ msg.sender|upper }} · {{ msg.channel|upper }} ·
        {{ msg.stage|upper }} · {{ msg.sent_at }}
      </div>
      <div class="transcript-content">{{ msg.content }}</div>
    </div>
    {% endfor %}
  </div>
  {% endif %}

  <!-- Footer -->
  <div class="footer">
    <span>J Turner Research · Leasing AI Audit Program</span>
    <span>Confidential · {{ generated_at }}</span>
  </div>

</div>
</body>
</html>
"""


class ReportGenerator:
    def __init__(self, output_dir: str = "reports/output"):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

        # Set up Jinja2 with custom filters
        self.env = Environment(loader=BaseLoader())
        self.env.filters["score_color"] = self._score_color
        self.env.filters["score_grade"] = self._score_grade
        self.env.globals["score_color"] = self._score_color
        self.env.globals["score_grade"] = self._score_grade

        logger.info(f"ReportGenerator initialized — output: {self.output_dir}")

    # -------------------------------------------------------------------------
    # MAIN ENTRY POINT
    # -------------------------------------------------------------------------

    def generate_property_report(
        self,
        property_id: str,
        orchestrator=None,
        include_transcript: bool = True,
    ) -> Optional[Path]:
        """
        Generates a full property-level report from completed engagements.
        Returns the path to the generated HTML file.
        """
        with get_db() as db:
            prop = db.query(Property).filter_by(id=property_id).first()
            if not prop:
                logger.error(f"Property {property_id} not found")
                return None

            engagements = db.query(Engagement).filter(
                Engagement.property_id == property_id,
                Engagement.status == EngagementStatus.COMPLETE
            ).all()

            if not engagements:
                logger.warning(f"No complete engagements found for {prop.name}")
                return None

            # Aggregate scores across all engagements
            aggregated = self._aggregate_scores(engagements, db)

            # Build timing stats
            timing = self._build_timing_stats(engagements)

            # Pull sample transcript from most recent engagement
            transcript = []
            if include_transcript and engagements:
                latest = engagements[-1]
                messages = db.query(Message).filter_by(
                    engagement_id=latest.id
                ).order_by(Message.sent_at).all()
                transcript = [
                    {
                        "sender": m.sender.value,
                        "channel": m.channel.value,
                        "stage": m.stage.value,
                        "content": m.content[:500],
                        "sent_at": m.sent_at.strftime("%b %d %H:%M") if m.sent_at else "",
                    }
                    for m in messages
                ]

            # Generate narrative via Gemini if orchestrator provided
            narrative = self._get_or_generate_narrative(
                prop=prop,
                aggregated=aggregated,
                engagements=engagements,
                orchestrator=orchestrator,
                db=db,
            )

            # Calculate derived metrics
            overall_score = self._calculate_overall(aggregated["scores"])
            handoff_index = self._calculate_handoff_index(aggregated["scores"])

            # Save/update PropertyReport record
            self._save_report_record(
                db=db,
                property_id=property_id,
                scores=aggregated["scores"],
                overall_score=overall_score,
                handoff_index=handoff_index,
                narrative=narrative,
                engagement_count=len(engagements),
            )

            # Render HTML
            html = self._render_html(
                prop=prop,
                scores=aggregated["scores"],
                rationales=aggregated["rationales"],
                overall_score=overall_score,
                handoff_index=handoff_index,
                timing=timing,
                narrative=narrative,
                transcript=transcript,
                engagement_count=len(engagements),
                persona_count=len(set(e.persona_id for e in engagements)),
            )

            # Write file
            filename = f"{prop.name.replace(' ', '_').lower()}_{datetime.now().strftime('%Y%m%d')}.html"
            output_path = self.output_dir / filename
            output_path.write_text(html, encoding="utf-8")

            logger.success(f"Report generated: {output_path}")
            return output_path

    # -------------------------------------------------------------------------
    # SCORING AGGREGATION
    # -------------------------------------------------------------------------

    def _aggregate_scores(self, engagements: list, db) -> dict:
        """
        Averages scores across all engagements for each dimension.
        Returns dict with scores and rationales.
        """
        dimension_scores: dict[str, list[float]] = {}
        dimension_rationales: dict[str, list[str]] = {}

        for engagement in engagements:
            scores = db.query(Score).filter_by(
                engagement_id=engagement.id
            ).all()
            for score in scores:
                dimension_scores.setdefault(score.dimension, []).append(score.score)
                if score.rationale:
                    dimension_rationales.setdefault(
                        score.dimension, []
                    ).append(score.rationale)

        averaged = {
            dim: sum(vals) / len(vals)
            for dim, vals in dimension_scores.items()
        }

        # Use most recent rationale per dimension for display
        rationales = {
            dim: notes[-1]
            for dim, notes in dimension_rationales.items()
        }

        return {"scores": averaged, "rationales": rationales}

    def _calculate_overall(self, scores: dict) -> float:
        if not scores:
            return 0.0
        return round(sum(scores.values()) / len(scores), 2)

    def _calculate_handoff_index(self, scores: dict) -> float:
        handoff_scores = [
            scores[dim]
            for dim in HANDOFF_INDEX_DIMENSIONS
            if dim in scores
        ]
        if not handoff_scores:
            return 0.0
        return round(sum(handoff_scores) / len(handoff_scores), 2)

    def _build_timing_stats(self, engagements: list) -> dict:
        response_times = [
            e.minutes_to_first_human_response
            for e in engagements
            if e.minutes_to_first_human_response is not None
        ]
        context_hits = [
            e for e in engagements
            if e.human_had_context is True
        ]

        return {
            "avg_human_response_minutes": (
                sum(response_times) / len(response_times)
                if response_times else None
            ),
            "context_continuity_rate": (
                int(len(context_hits) / len(engagements) * 100)
                if engagements else 0
            ),
        }

    # -------------------------------------------------------------------------
    # NARRATIVE
    # -------------------------------------------------------------------------

    def _get_or_generate_narrative(
        self,
        prop,
        aggregated: dict,
        engagements: list,
        orchestrator,
        db,
    ) -> str:
        # Check if we already have a saved narrative
        existing = db.query(PropertyReport).filter_by(
            property_id=prop.id
        ).order_by(PropertyReport.generated_at.desc()).first()

        if existing and existing.narrative_summary:
            return existing.narrative_summary

        if not orchestrator:
            return self._fallback_narrative(prop.name, aggregated["scores"])

        # Generate via Gemini
        engagement_notes = [
            e.orchestrator_notes for e in engagements
            if e.orchestrator_notes
        ]
        return orchestrator.generate_property_narrative(
            property_name=prop.name,
            scores=aggregated["scores"],
            engagement_notes=engagement_notes,
        )

    def _fallback_narrative(self, property_name: str, scores: dict) -> str:
        overall = self._calculate_overall(scores)
        handoff = self._calculate_handoff_index(scores)
        return (
            f"{property_name} received an overall communication score of "
            f"{overall:.1f}/5.0 across all evaluated dimensions. "
            f"The Handoff Index — J Turner's signature metric measuring the "
            f"quality of the AI-to-human transition — scored {handoff:.1f}/5.0. "
            f"A full narrative summary will be generated once Gemini scoring "
            f"is complete for all engagements."
        )

    # -------------------------------------------------------------------------
    # RENDERING
    # -------------------------------------------------------------------------

    def _render_html(
        self,
        prop,
        scores: dict,
        rationales: dict,
        overall_score: float,
        handoff_index: float,
        timing: dict,
        narrative: str,
        transcript: list,
        engagement_count: int,
        persona_count: int,
    ) -> str:
        template = self.env.from_string(REPORT_TEMPLATE)

        # Split narrative into paragraphs
        narrative_paragraphs = [
            p.strip() for p in narrative.split("\n\n") if p.strip()
        ]

        return template.render(
            property=prop,
            scores=scores,
            rationales=rationales,
            dimensions=DIMENSION_LABELS,
            overall_score=overall_score,
            overall_grade=self._score_grade(overall_score),
            overall_color=self._score_color(overall_score),
            handoff_index=handoff_index,
            handoff_color=self._score_color(handoff_index),
            avg_human_response_minutes=timing["avg_human_response_minutes"],
            context_continuity_rate=timing["context_continuity_rate"],
            engagement_count=engagement_count,
            persona_count=persona_count,
            narrative_paragraphs=narrative_paragraphs,
            transcript=transcript,
            generated_at=datetime.now().strftime("%B %d, %Y at %H:%M UTC"),
        )

    # -------------------------------------------------------------------------
    # DATABASE
    # -------------------------------------------------------------------------

    def _save_report_record(
        self,
        db,
        property_id: str,
        scores: dict,
        overall_score: float,
        handoff_index: float,
        narrative: str,
        engagement_count: int,
    ):
        report = PropertyReport(
            property_id=property_id,
            score_ai_responsiveness=scores.get("ai_responsiveness"),
            score_ai_accuracy=scores.get("ai_accuracy"),
            score_handoff_communication=scores.get("handoff_communication"),
            score_context_continuity=scores.get("context_continuity"),
            score_human_response_speed=scores.get("human_response_speed"),
            score_human_quality=scores.get("human_quality"),
            score_handoff_index=handoff_index,
            score_overall=overall_score,
            narrative_summary=narrative,
            engagement_count=engagement_count,
            generated_at=datetime.now(timezone.utc),
        )
        db.add(report)

    # -------------------------------------------------------------------------
    # HELPERS
    # -------------------------------------------------------------------------

    def _score_grade(self, score: float) -> str:
        for (low, high), (grade, _) in GRADE_THRESHOLDS.items():
            if low <= score <= high:
                return grade
        return "N/A"

    def _score_color(self, score: float) -> str:
        for (low, high), (_, color) in GRADE_THRESHOLDS.items():
            if low <= score <= high:
                return color
        return "#6b7280"
