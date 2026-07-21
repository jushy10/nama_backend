# Container image for the FastAPI app, run on ECS Fargate.
#
# Bumping this interpreter? Bump .github/workflows/tests.yml's python-version
# with it — CI runs the suite on its own runner, not in this image, so the two
# only stay aligned by hand.
FROM python:3.12-slim

WORKDIR /app

# Install the app + Postgres driver + the Bedrock (Anthropic) SDK for the AI
# analysis endpoint. Copying just the metadata + package keeps the build small
# and cache-friendly.
COPY pyproject.toml ./
COPY app ./app
RUN pip install --no-cache-dir ".[postgres,bedrock]"

# Migration config, so `alembic upgrade head` can run from this image (a one-off
# ECS task in the VPC against RDS on each deploy). Copied after the install so
# editing it doesn't bust the dependency layer.
COPY alembic.ini ./
COPY alembic ./alembic

EXPOSE 8000

# DATABASE_URL is injected by ECS from SSM; the app reads it at startup.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
