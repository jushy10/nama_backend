# Container image for the FastAPI app, run on ECS Fargate.
FROM python:3.12-slim

WORKDIR /app

# Install the app + Postgres driver + the Bedrock (Anthropic) SDK for the AI
# analysis endpoint. Copying just the metadata + package keeps the build small
# and cache-friendly.
COPY pyproject.toml ./
COPY app ./app
RUN pip install --no-cache-dir ".[postgres,bedrock]"

# Migration config + ops scripts, so `alembic upgrade head` and the constituents
# sync can run from this image (e.g. one-off ECS tasks in the VPC against RDS).
# Copied after the install so editing them doesn't bust the dependency layer.
COPY alembic.ini ./
COPY alembic ./alembic
COPY scripts ./scripts

EXPOSE 8000

# DATABASE_URL is injected by ECS from SSM; the app reads it at startup.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
