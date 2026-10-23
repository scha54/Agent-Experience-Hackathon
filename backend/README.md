# CraveQuest Backend

Python/FastAPI backend for CraveQuest: a genuine Strands Decider 2B cooking-strategy
decision (`#tool-selection`) followed by Amazon Bedrock recipe generation.

## Layout

```
app/
  main.py            FastAPI app (CORS, body-size limits, routers)
  config.py          env-driven settings + startup validation
  api/               health, recipes, metrics routers
  agent/             Strands_Agent orchestration (decision -> single tool)
  tools/             quick_recipe, leftover_remix, no_cook_snack
  decision_patterns/ reusable cooking-strategy decision module (#tool-selection)
  models/            Pydantic v2 recipe/request/response models
  services/          decider adapter, bedrock generator, metrics collector
  safety/            deterministic dietary safety checker
tests/               pytest + Hypothesis test suite
```

## Setup

```bash
python -m venv .venv
# Windows PowerShell: .venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt
cp .env.example .env   # then fill in required values
```

## Running tests

```bash
pytest
```

## Configuration

See `.env.example` for every supported environment variable. Required variables
without defaults (`DECIDER_MODEL`, `BEDROCK_MODEL_ID`, `ALLOWED_ORIGINS`) must be
set or the app fails startup. Never commit a real `.env`.
