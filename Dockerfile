# Container image for the FastAPI app, run on ECS Fargate.
FROM python:3.12-slim

WORKDIR /app

# Install the app + Postgres driver. Copying just the metadata + package keeps
# the build small and cache-friendly.
COPY pyproject.toml ./
COPY app ./app
RUN pip install --no-cache-dir ".[postgres]"

EXPOSE 8000

# DATABASE_URL is injected by ECS from SSM; the app reads it at startup.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
