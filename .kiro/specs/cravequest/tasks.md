# Implementation Plan: CraveQuest

## Overview

This plan converts the CraveQuest design into incremental, test-driven coding tasks. It builds the backend
decision/generation pipeline bottom-up: configuration and typed models first, then the deterministic safety
and decision logic, then the strategy tools and Bedrock generation, then the agent wiring and HTTP API. The
React frontend is built against the typed API, followed by the reusable decision-pattern deliverable, the
benchmark harness, the AWS infrastructure, and the documentation/demo deliverables. Property-based tests for
the eight Correctness Properties are placed next to the logic they validate so invariants are caught early;
unit, integration, and smoke tests follow the Testing Strategy. The backend is Python/FastAPI with Pydantic
v2 and Hypothesis; the frontend is React + TypeScript + Vite; infrastructure is AWS CDK or SAM.

## Tasks

- [x] 1. Set up backend project structure and configuration
  - [x] 1.1 Create backend package layout and tooling
    - Create the `backend/app` package tree (`api/`, `agent/`, `tools/`, `decision_patterns/`, `models/`, `services/`, `safety/`) and `backend/tests/` per the module layout
    - Add `pyproject.toml`/`requirements.txt` with FastAPI, Pydantic v2, boto3, litellm, pytest, and Hypothesis; add a `.env.example` with placeholder values and no secrets
    - _Requirements: 33.1, 26.3_

  - [x] 1.2 Implement env-driven settings with startup validation
    - Write `app/config.py` reading every env var in the design config table (decider mode/checkpoint/base/key, decision timeout, confidence threshold, Bedrock model id/retries/backoff/timeout, max time/servings/string/list, body sizes, CORS origins, session limit, no-cook prep, extra-ingredient caps, explanation length)
    - Fail startup and report the offending value when the confidence threshold is outside 0.0–1.0, or when a required no-default var (`DECIDER_MODEL`, `BEDROCK_MODEL_ID`, `ALLOWED_ORIGINS`) is missing
    - Reconcile Bedrock retries to a single `BEDROCK_MAX_RETRIES` default of 3
    - _Requirements: 3.1, 3.2, 3.3, 9.3, 10.3, 15.1, 26.3, 27.1, 34.4_

  - [ ]* 1.3 Write unit tests for config startup validation
    - Assert threshold accepted within [0.0, 1.0] and startup fails outside it with the out-of-range value reported
    - Assert missing required env vars without defaults fail startup
    - _Requirements: 3.3, 9.3, 15.1_

- [x] 2. Implement typed data models
  - [x] 2.1 Implement Strategy/DecisionSource enums and Recipe_Schema models
    - Write `models/recipe.py` with `Difficulty`, `Ingredient` (non-empty `quantity`), and `Recipe` (all required fields, `strategy`, non-negative times, `servings >= 1`, `reasoning_summary` max 600) per the Pydantic v2 schema
    - Place `Strategy` and `DecisionSource` enums where models and the adapter can share them
    - _Requirements: 6.2, 9.2, 9.4, 9.5_

  - [x] 2.2 Implement request and response models
    - Write `models/requests.py` (`GenerateRequest`/`RegenerateRequest`) enforcing enums, ranges, non-empty ingredients, and per-string/per-list caps with field validators
    - Write `models/responses.py` (`GenerateResponse` with decision metadata, optional `decision_confidence`, `is_mock`, `request_id`) and safe error models
    - _Requirements: 1.1, 1.3, 1.4, 1.5, 1.6, 12.1, 12.2, 12.3, 34.3_

  - [ ]* 2.3 Write property test for Recipe_Schema validation
    - **Property 6: Every validated recipe conforms to Recipe_Schema**
    - **Validates: Requirements 9.4, 9.5**
    - Tag comment: `Feature: cravequest, Property 6`

  - [ ]* 2.4 Write unit tests for request and recipe models
    - Test rejection of missing fields, out-of-enum values, out-of-range numbers, empty ingredients, over-length strings/lists, out-of-set difficulty, and negative times
    - _Requirements: 1.2, 1.3, 1.4, 1.5, 1.6, 9.5_

- [x] 3. Implement the Dietary_Safety_Checker
  - [x] 3.1 Implement normalization and pre-generation exclusion checks
    - Write `safety/dietary_safety_checker.py` with deterministic normalization (lowercase/trim + canonical alias map), fail-safe handling of unresolved names, and a pre-generation exclusion check against dietary preferences and allergens
    - Expose active restrictions so they can be passed to Bedrock as constraints
    - _Requirements: 23.1, 23.2, 23.3, 23.4_

  - [x] 3.2 Implement post-generation recipe validation
    - Validate every returned ingredient and instruction against exclusions through the canonical alias map; reject on any excluded or ambiguous match; never relax a restriction for creativity; never emit unverified medical claims
    - _Requirements: 24.1, 24.2, 24.4, 25.2_

  - [ ]* 3.3 Write property test for safety validation
    - **Property 4: No excluded or allergen ingredient survives safety validation**
    - **Validates: Requirements 6.1, 7.1, 8.3, 23.1, 23.2, 23.3, 24.1, 24.2, 24.4**
    - Tag comment: `Feature: cravequest, Property 4`

  - [ ]* 3.4 Write unit tests for the safety checker
    - Test alias resolution, unresolved-name fail-safe, and pre/post matching against ingredients and instructions
    - _Requirements: 23.1, 23.2, 24.1_

- [x] 4. Implement the Metrics_Collector
  - [x] 4.1 Implement separate decision/generation instrumentation
    - Write `services/metrics_collector.py` recording request count, strategy selection counts, Decider latency, Bedrock latency, end-to-end latency, decision failures, fallback rate, and recipe validation failures as separately identified stages, with decision latency distinct from total agent latency
    - _Requirements: 30.1, 30.2, 30.3_

  - [ ]* 4.2 Write property test for latency reporting
    - **Property 7: Latencies are non-negative integers reported as distinct stages**
    - **Validates: Requirements 2.6, 30.3**
    - Tag comment: `Feature: cravequest, Property 7`

- [x] 5. Implement the DeciderAdapter
  - [x] 5.1 Define the adapter interface and DecisionResult
    - Write `services/decider_adapter.py` with the `DeciderAdapter` Protocol, `DecisionResult` dataclass, and a `DeciderError` type; document the interface
    - _Requirements: 34.1_

  - [x] 5.2 Implement RealDeciderAdapter against the Strands Decider provider
    - Call the Decider_2B server via the LiteLLM `systemone` `choice` question over exactly the three options using `STRANDS_DECIDER_API_BASE`/`STRANDS_DECIDER_API_KEY` and the configured checkpoint (no hardcoded ids); map `choice` to `Strategy`, capture `confidence` (0.0–1.0) and client/server `latency_ms`; raise `DeciderError` on timeout, transport error, or out-of-set answer
    - _Requirements: 2.1, 2.3, 2.5, 2.6, 34.4_

  - [x] 5.3 Implement MockDeciderAdapter (Mock_Mode)
    - Return a deterministic labeled result with `is_mock=True` and no genuine confidence, enabled only by explicit `DECIDER_MODE=mock`; never present mock output as genuine inference
    - _Requirements: 34.2, 34.3_

  - [ ]* 5.4 Write unit tests for both adapters
    - Test Real choice-to-Strategy mapping and error raising on timeout/transport/out-of-set; test Mock returning a labeled result with no confidence
    - _Requirements: 2.3, 2.5, 34.2, 34.3_

- [x] 6. Implement the Decision_Module (decision_patterns/cooking_strategy.py)
  - [x] 6.1 Implement strategy selection, threshold, and deterministic fallback
    - Write `decision_patterns/cooking_strategy.py` (`#tool-selection`) that calls the adapter, validates the answer is one of the three options, applies the configurable confidence threshold (records `below_threshold` without forcing fallback), sets `source=decider_2b` on success, and applies the deterministic `Fallback_Strategy` (documented rule) only on timeout/transport/out-of-set with `source=fallback`
    - Record a decision failure (not a successful inference) via the Metrics_Collector on fallback
    - _Requirements: 2.2, 2.4, 2.7, 2.8, 3.1, 3.4, 4.1, 4.2, 4.3, 4.5_

  - [ ]* 6.2 Write property test for fallback-iff-failure
    - **Property 2: Fallback is applied if and only if inference fails**
    - **Validates: Requirements 2.4, 4.1, 4.2, 4.3, 4.5**
    - Tag comment: `Feature: cravequest, Property 2`

  - [ ]* 6.3 Write unit tests for the fallback rule branches
    - Test no-cook preference, low energy + short time, low budget + many ingredients, and default branches produce the expected Strategy
    - _Requirements: 4.1, 4.2_

- [x] 7. Implement the Bedrock_Generator
  - [x] 7.1 Implement Converse Tool Use generation with Recipe_Schema
    - Write `services/bedrock_generator.py` using boto3 `bedrock-runtime` `converse` with a `toolConfig` whose `inputSchema.json` is the Recipe_Schema JSON Schema and `toolChoice` forcing a single `tool_use` block; read `BEDROCK_MODEL_ID` from env; supply the selected Strategy and active dietary restrictions as constraints; set `Recipe_Schema.strategy`; validate the result with Pydantic; cap `reasoning_summary` and never expose chain-of-thought
    - _Requirements: 9.1, 9.2, 9.3, 9.4, 9.6, 23.4_

  - [x] 7.2 Implement retry/backoff and safe failure handling
    - Treat throttling, service errors, per-attempt timeout (`BEDROCK_ATTEMPT_TIMEOUT_MS`), empty content, malformed JSON, and schema-invalid results as failures; retry up to `BEDROCK_MAX_RETRIES` (default 3) with exponential backoff (500→8000 ms); raise a safe `GenerationError` on exhaustion with no stack traces, credentials, or prompts
    - Integrate post-generation dietary validation with regeneration up to 3 attempts, then a safety error
    - _Requirements: 10.1, 10.2, 10.3, 10.4, 10.5, 24.2, 24.3, 25.3, 27.1, 27.2_

  - [ ]* 7.3 Write property test for strategy recording
    - **Property 8: The recipe records the strategy that was selected**
    - **Validates: Requirements 9.2**
    - Tag comment: `Feature: cravequest, Property 8`

  - [ ]* 7.4 Write unit tests for Bedrock failure mapping
    - With a mocked `bedrock-runtime` client, assert throttling/timeout/invalid/malformed retries up to the configured max with backoff then a safe error, and that dietary rejection drives up to 3 regenerations then a safety error
    - _Requirements: 10.1, 10.2, 10.3, 10.4, 10.5, 24.2, 24.3_

- [x] 8. Checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 9. Implement the three Strategy_Tools
  - [x] 9.1 Implement quick_recipe tool
    - Write `tools/quick_recipe.py` as a Strands `@tool` enforcing combined prep+cook ≤ available time, exclusions respected, equipment limited to request, `servings` equals request, non-empty quantities, all request ingredients `is_available=true`, and extra ingredients capped at `QUICK_MAX_EXTRA_INGREDIENTS` (5); delegate content to the Bedrock_Generator
    - _Requirements: 6.1, 6.2, 6.3_

  - [x] 9.2 Implement leftover_remix tool with its invariants
    - Write `tools/leftover_remix.py` marking every request ingredient `is_available=true`, capping extra ingredients at `LEFTOVER_MAX_EXTRA_INGREDIENTS` (3), recording substitutions only for items absent from the request (7.2), enforcing a non-empty `reasoning_summary` ≤ `MAX_EXPLANATION_CHARS` that references ≥1 request ingredient and regenerating otherwise (7.4), and recording a request-only creative twist in `bonus_challenge` when present (7.5)
    - _Requirements: 7.1, 7.2, 7.3, 7.4, 7.5_

  - [x] 9.3 Implement no_cook_snack tool
    - Write `tools/no_cook_snack.py` forcing `cook_time_minutes == 0` with no heat step, `prep_time_minutes` ≤ `NO_COOK_MAX_PREP_MINUTES` (10), equipment limited to request, and exclusions respected; re-assert hard invariants after validation
    - _Requirements: 8.1, 8.2, 8.3_

  - [ ]* 9.4 Write property test for no-cook invariants
    - **Property 5: No-cook recipes require no cooking and stay within the prep limit**
    - **Validates: Requirements 8.1, 8.2**
    - Tag comment: `Feature: cravequest, Property 5`

  - [ ]* 9.5 Write unit tests for strategy-tool constraints
    - Test quick_recipe time/extra-ingredient caps, leftover_remix substitution/reasoning/bonus invariants, and no_cook equipment/exclusion handling with a mocked generator
    - _Requirements: 6.1, 6.3, 7.2, 7.4, 7.5, 8.3_

- [x] 10. Implement the Strands_Agent orchestration
  - [x] 10.1 Wire the decision to exactly one tool
    - Write `agent/orchestrator.py` with `TOOL_BY_STRATEGY` mapping each Strategy to exactly one tool, invoking exactly one tool per request, and assembling the `GenerateResponse` (recipe, selected_strategy, decision metadata, latencies, request_id, optional confidence, mock indicator)
    - _Requirements: 5.1, 5.2, 5.3, 5.4, 5.5, 5.6_

  - [ ]* 10.2 Write property test for single-tool routing
    - **Property 1: The decision maps to exactly one strategy and exactly one tool**
    - **Validates: Requirements 2.2, 2.8, 5.1, 5.2, 5.3, 5.4, 5.5**
    - Tag comment: `Feature: cravequest, Property 1`

  - [ ]* 10.3 Write property test for decision_confidence presence
    - **Property 3: decision_confidence is present only for a genuine Decider_2B decision**
    - **Validates: Requirements 4.4, 12.2, 12.3, 34.3**
    - Tag comment: `Feature: cravequest, Property 3`

- [x] 11. Implement the Backend_API endpoints and middleware
  - [x] 11.1 Implement the FastAPI app, CORS, and body-size limits
    - Write `app/main.py` wiring routers, env-driven CORS origins, the global 256 KB and per-endpoint 64 KB body caps (413), per-session request limit, and safe error handlers that exclude stack traces/credentials/prompts and do not persist rejected input or personal data
    - _Requirements: 1.7, 15.1, 15.2, 15.3, 26.4, 26.5, 26.6, 26.8, 27.3, 27.4_

  - [x] 11.2 Implement /health and /api/metrics/summary
    - Write `api/health.py` returning only status + timestamp (no secrets/config) and `api/metrics.py` returning aggregated decision counts, source distribution, and latency summaries, protected by a credential check or excluded from public deploy
    - _Requirements: 11.1, 11.2, 14.1, 14.2, 14.3_

  - [x] 11.3 Implement /api/recipes/generate and /regenerate
    - Write `api/recipes.py` calling the orchestrator with pre-generation safety and returning recipe + decision metadata on generate; on regenerate return a new recipe for the same strategy, changing strategy only on explicit user request or a new decision; reject malformed bodies with field-identifying errors without generating
    - _Requirements: 12.1, 12.4, 12.5, 13.1, 13.2, 13.3, 13.4_

  - [ ]* 11.4 Write integration tests for the endpoints
    - With Decider and Bedrock mocked, assert generate/regenerate responses carry recipe, selected_strategy, decision_source, latencies, and request_id; invalid bodies yield field-identifying 422 without generating; /health exposes only status+timestamp; /api/metrics/summary rejects unauthenticated access
    - _Requirements: 11.1, 11.2, 12.1, 12.5, 13.1, 13.4, 14.1, 14.3_

- [x] 12. Checkpoint - Ensure all backend tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 13. Implement the Frontend_App shell and API client
  - [x] 13.1 Scaffold React + TS + Vite app with typed API client
    - Create the `frontend/` Vite + React + TypeScript project; write a typed API client for generate/regenerate with request/response types mirroring the backend models; no AWS credentials in source or config
    - _Requirements: 17.5, 26.2_

  - [x] 13.2 Implement routing and shared layout with accessibility baseline
    - Implement view routing (Landing/Selection/Loading/Result/Error), keyboard operability, visible focus indicators, and WCAG 2.1 AA contrast tokens (warm cream background, fresh-food accents, rounded cards)
    - _Requirements: 21.1, 21.2, 21.3_

- [x] 14. Implement the Frontend_App views
  - [x] 14.1 Implement Landing view
    - Render logo, tagline, short explanation, and a primary CTA labeled exactly "Find my next bite" that navigates to the selection view
    - _Requirements: 16.1, 16.2_

  - [x] 14.2 Implement Selection view
    - Add/remove ingredient chips capped at the max list count; provide time, energy, craving, equipment, budget, and servings controls; display dietary preference and allergen fields prominently with review before submit; submit to `/api/recipes/generate`
    - _Requirements: 17.1, 17.2, 17.3, 17.4, 17.5_

  - [x] 14.3 Implement Loading view with honesty
    - Show friendly loading messages and loading indicators; never claim genuine Decider_2B inference for a mock or fallback result
    - _Requirements: 18.1, 18.2, 21.4_

  - [x] 14.4 Implement Result view
    - Display title, quest title, total time, difficulty, servings, strategy badge, available vs additional ingredient groups, numbered steps, quest description, bonus challenge, match explanation; provide regenerate, save/favorite, and start-cooking/mark-complete controls; advise verifying product labels and avoid allergy-safe claims
    - _Requirements: 19.1, 19.2, 19.3, 19.4, 25.1_

  - [x] 14.5 Implement Error states and empty states
    - Map missing-ingredients (client-side, no request), invalid-input, backend-unavailable, model-access-not-configured, generation-timeout, and invalid-recipe to distinct messages that retain entered input; render empty states when no content
    - _Requirements: 20.1, 20.2, 20.3, 20.4, 20.5, 20.6, 21.4_

  - [x] 14.6 Implement local persistence of favorites and preferences
    - Persist favorites (capped at the max favorites count, default 50) and lightweight preferences to browser local storage; load on return; default to empty state without error when storage is empty
    - _Requirements: 22.1, 22.2, 22.3, 22.4, 22.5_

  - [ ]* 14.7 Write frontend component tests
    - Test the exact CTA label and navigation, ingredient chip cap, error-state mapping with input retention, and local-storage load/empty-state behavior
    - _Requirements: 16.1, 16.2, 17.2, 20.1, 22.5_

- [x] 15. Package the reusable Decision_Pattern_Deliverable
  - [x] 15.1 Make the pattern standalone with adaptation example
    - Ensure `decision_patterns/cooking_strategy.py` depends only on the `DeciderAdapter` interface and a caller-supplied option-to-handler map (no Strategy_Tools/Bedrock/FastAPI coupling); add a minimal adaptation example runnable with a trivial handler map; carry the `#tool-selection` tag
    - _Requirements: 33.1, 33.3, 33.4_

  - [ ]* 15.2 Write tests for standalone pattern usage
    - Test the pattern executes with a trivial option-to-handler map and a mock adapter without any CraveQuest-specific component
    - _Requirements: 33.4_

  - [ ]* 15.3 Write the decision-pattern README
    - Document the decision problem, typed choice question, options and meanings, `MealContext` schema, Decider_2B API/integration, agent wiring, option-to-tool mapping, error/fallback handling, test instructions, keyword-filter limitations, and a minimal adaptation example; include `#tool-selection`
    - _Requirements: 25.4, 33.2, 33.3_

- [x] 16. Implement the Benchmark_Script
  - [x] 16.1 Implement benchmark_decisions.py
    - Write `scripts/benchmark_decisions.py` comparing Decider_2B strategy selection against a named baseline using identical inputs, a configurable sample size (default 100 per subject), reporting sample count, median, and p95 per measurement
    - _Requirements: 31.1, 31.2, 31.3_

- [x] 17. Implement AWS infrastructure as code
  - [x] 17.1 Define the CDK/SAM stack
    - Define Amplify hosting for the frontend, API Gateway fronting the Backend_API, a container service (ECS Fargate / App Runner) for Backend_API + Decider_2B server, CloudWatch log groups/metrics, least-privilege IAM (no wildcard/admin actions; Bedrock Converse + own-log-group writes only), and stack outputs for the frontend URL and API URL
    - _Requirements: 26.1, 28.1, 28.2, 28.4, 28.5, 28.6, 28.8, 28.9_

  - [ ]* 17.2 Write infrastructure synthesis/smoke tests
    - Assert the stack synthesizes and asserts key resources (Amplify, API Gateway, container service, CloudWatch) and least-privilege IAM (no wildcard actions) and defines both stack outputs
    - _Requirements: 26.1, 28.9_

- [x] 18. Write project documentation and demo scenario
  - [x]* 18.1 Write deployment and developer documentation
    - Write a deployment guide, env var reference (every config-table var with defaults/meaning), local dev instructions (including `DECIDER_MODE=mock`), credential setup, Bedrock model-access instructions, deploy/teardown commands, troubleshooting, Lambda-vs-container rationale, budget-monitoring checklist, and paid-resource inventory that never claims free deployment
    - _Requirements: 27.5, 27.6, 28.4, 29.1, 31.4_

  - [x]* 18.2 Write the demo scenario
    - Document a scripted demo completable in 180 seconds or less with exact inputs, covering the honest loading state and the decision metadata (genuine vs fallback/mock)
    - _Requirements: 32.1_

- [x] 19. Final checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional and can be skipped for a faster MVP; they include unit, property,
  integration, and smoke tests, infrastructure tests, and prose documentation deliverables.
- Each task references specific granular requirement clauses for traceability.
- Checkpoints ensure incremental validation at natural boundaries.
- Property-based tests (Hypothesis, ≥100 iterations each, one test per property) validate the eight
  Correctness Properties and are placed next to the logic they cover; unit and integration tests cover
  concrete behavior, edge cases, and endpoint wiring per the Testing Strategy.
- The backend is Python/FastAPI with Pydantic v2; the frontend is React + TypeScript + Vite; infrastructure
  is AWS CDK or SAM.

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1"] },
    { "id": 1, "tasks": ["1.2", "1.3"] },
    { "id": 2, "tasks": ["2.1", "2.2"] },
    { "id": 3, "tasks": ["2.3", "2.4", "3.1", "4.1", "5.1"] },
    { "id": 4, "tasks": ["3.2", "3.3", "3.4", "4.2", "5.2", "5.3"] },
    { "id": 5, "tasks": ["5.4", "6.1", "7.1"] },
    { "id": 6, "tasks": ["6.2", "6.3", "7.2", "7.3"] },
    { "id": 7, "tasks": ["7.4", "9.1", "9.2", "9.3"] },
    { "id": 8, "tasks": ["9.4", "9.5", "10.1"] },
    { "id": 9, "tasks": ["10.2", "10.3", "11.1", "11.2", "11.3"] },
    { "id": 10, "tasks": ["11.4", "13.1", "13.2"] },
    { "id": 11, "tasks": ["14.1", "14.2", "14.3", "14.4", "14.5", "14.6"] },
    { "id": 12, "tasks": ["14.7", "15.1", "16.1", "17.1"] },
    { "id": 13, "tasks": ["15.2", "15.3", "17.2", "18.1", "18.2"] }
  ]
}
```
