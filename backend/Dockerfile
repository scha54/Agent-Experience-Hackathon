# CraveQuest Backend_API + Decider_2B container image.
#
# WHY A CONTAINER (not AWS Lambda): the Decider_2B server loads 2B-parameter
# model weights and runs a long-lived serving process that must stay warm
# across requests. That exceeds Lambda's package-size (250 MB), memory, and
# 15-minute duration limits, and reloading weights per cold start is
# impractical. See infra/lib/cravequest-stack.ts and design.md (Req 28.3/28.4).
#
# This image bundles the FastAPI Backend_API. In the full deployment the
# Decider_2B serving process (`strands-decider serve`) runs alongside it and is
# reached via STRANDS_DECIDER_API_BASE; add its install/launch steps here when
# the Decider package is available to the build.

FROM python:3.11-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Install runtime dependencies first for better layer caching.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Copy the application package.
COPY app ./app
COPY pyproject.toml ./

# The Backend_API listens on 8080 (matches the ECS task port mapping and the
# ALB target / API Gateway integration in the CDK stack).
EXPOSE 8080

# NEVER bake secrets into the image. Configuration is supplied at runtime via
# environment variables / secret manager (Req 26.3).

# Run the FastAPI Backend_API. uvicorn is declared in requirements.txt.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
