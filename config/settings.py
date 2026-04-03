import os
from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    # GCP
    project_id: str = os.environ.get("GCP_PROJECT_ID", "")
    region: str = os.environ.get("GCP_REGION", "us-central1")

    # Vertex AI / Gemini
    gemini_model: str = "gemini-2.5-pro"
    gemini_temperature: float = 0.7
    gemini_max_tokens: int = 2048

    # Cloud SQL
    db_instance: str = os.environ.get("DB_INSTANCE_CONNECTION_NAME", "")
    db_name: str = os.environ.get("DB_NAME", "leasing_auditor")
    db_user: str = os.environ.get("DB_USER", "auditor")
    db_password: str = os.environ.get("DB_PASSWORD", "")

    # Gmail API
    gmail_credentials_secret: str = "gmail-api-credentials"

    # Engagement config
    handoff_wait_hours: int = 72
    human_response_benchmark_hours: int = 4

    class Config:
        env_file = ".env"

settings = Settings()
