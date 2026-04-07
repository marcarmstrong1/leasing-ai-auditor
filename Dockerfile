FROM mcr.microsoft.com/playwright/python:v1.43.0-jammy

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    ENVIRONMENT=production

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p logs reports/output

ENTRYPOINT ["python3", "-m", "agent.pipeline"]
CMD ["--help"]
