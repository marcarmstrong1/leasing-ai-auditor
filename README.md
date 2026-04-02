# Leasing AI Auditor

**Built by J Turner Research**

An AI-powered mystery shopper system that evaluates multifamily property leasing communication quality — specifically the AI chatbot experience, the AI-to-human handoff, and the human follow-up response.

## What It Measures

| Dimension | Description |
|---|---|
| AI Responsiveness | Speed and completeness of chatbot answers |
| AI Accuracy | Factual correctness of chatbot responses |
| Handoff Communication | Did the AI signal the transition clearly? |
| Context Continuity | Did the human rep have prior conversation context? |
| Human Response Speed | Time delta vs. AI speed |
| Human Quality | Warmth, relevance, and completeness of human response |

## Architecture

- **Runtime**: Google Cloud Run
- **AI Brain**: Vertex AI / Gemini 1.5 Pro
- **Browser Automation**: Playwright (headless Chrome)
- **Email**: Gmail API
- **Database**: Cloud SQL (Postgres)
- **Reporting**: Looker Studio + generated PDFs
- **Scheduling**: Cloud Scheduler

## Personas

The auditor engages properties using realistic renter personas, each with a consistent backstory, budget, timeline, and conversation arc designed to probe the AI-to-human handoff.

## Project Status

🚧 Active development — MVP targeting property-level scoring reports.
