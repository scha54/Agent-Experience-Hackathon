# CraveQuest

CraveQuest is an AI-powered food-adventure web app. Its defining feature is a
genuine, observable cooking-strategy decision made by **Strands Decider 2B**
(hackathon pattern tag `#tool-selection`), followed by generative recipe creation
on **Amazon Bedrock**. Tell it what you have, how much time and energy you've got,
your craving, budget, equipment, and dietary needs â€” it picks one of three cooking
strategies and generates a playful, quest-themed recipe tailored to your real
situation.

The decision is real: Decider_2B chooses exactly one strategy from
`QUICK_RECIPE`, `LEFTOVER_REMIX`, or `NO_COOK_SNACK`, and that choice
deterministically routes to exactly one strategy tool. When inference fails, a
clearly labeled deterministic fallback is used instead â€” and the app never
presents a fallback or mock result as a genuine inference.

## Components

| Path | What it is |
|---|---|
| [`backend/`](./backend/README.md) | Python/FastAPI service: decision module, three strategy tools, Bedrock generator, deterministic dietary safety checker, metrics. |
| [`frontend/`](./frontend) | React + TypeScript + Vite web client. |
| [`infra/`](./infra/README.md) | AWS CDK (TypeScript) stack: Amplify, API Gateway, container service, CloudWatch, least-privilege IAM. |
| [`scripts/`](./scripts) | `benchmark_decisions.py` â€” reproducible Decider-vs-baseline measurement harness. |
| [`docs/`](./docs) | Deployment & developer guide and the scripted demo. |

## Quick start (local)

```bash
# Backend
cd backend
python -m venv .venv && .venv\Scripts\Activate.ps1   # PowerShell
pip install -r requirements-dev.txt
cp .env.example .env            # set DECIDER_MODEL, BEDROCK_MODEL_ID, ALLOWED_ORIGINS
uvicorn app.main:app --reload --port 8000

# Frontend (in another terminal)
cd frontend
npm install
cp .env.example .env            # set VITE_API_BASE_URL
npm run dev
```

No Decider server yet? Set `DECIDER_MODE=mock` in `backend/.env` to exercise the
full path with a clearly labeled mock decision. Full details, including the
environment variable reference, credential setup, Bedrock model access, deploy
and teardown commands, the Lambda-vs-container rationale, troubleshooting, and
cost controls, are in the guide below.

## Documentation

- **[Deployment & Developer Guide](./docs/DEPLOYMENT.md)** â€” run locally, deploy
  to AWS, tear down, env var reference, budget monitoring, troubleshooting.
- **[Demo scenario](./docs/demo-scenario.md)** — a scripted end-to-end demo
  completable in under 180 seconds.

## Cost notice

Deploying CraveQuest to AWS creates **paid** resources â€” Amazon Bedrock, a
container service (ECS Fargate / App Runner), Amazon API Gateway, AWS Amplify
Hosting, and Amazon CloudWatch. **This deployment is not free of charge.** Set an
AWS Budget and billing alarm before deploying, and tear the stack down when you
are not demoing. See the
[paid-resource inventory](./docs/DEPLOYMENT.md#paid-resource-inventory) and
[budget-monitoring checklist](./docs/DEPLOYMENT.md#budget-monitoring-checklist).
