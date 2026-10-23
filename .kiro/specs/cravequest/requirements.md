# Requirements Document

## Introduction

CraveQuest is an AI-powered food adventure web application built for the AWS Agent Experience Hackathon (Strands Decider track). It helps people decide what to eat based on ingredients they already have, available time, cravings, cooking energy, budget, equipment, and dietary preferences. Unlike a generic recipe chatbot, CraveQuest's defining feature is an agent that makes a meaningful, explicit cooking-strategy decision using Strands Decider 2B (hackathon pattern tag `#tool-selection`), then uses Amazon Bedrock for generative recipe creation. The experience is playful (quests, narratives, bonus challenges) while remaining accessible and useful for everyday life.

This document specifies the requirements for the decision module, strategy tools, Bedrock generation layer, API endpoints, frontend experience, dietary safety handling, security/privacy/cost controls, AWS deployment, measurement/benchmarking, and the reusable decision-pattern deliverable.

## Glossary

- **CraveQuest**: The overall food-adventure web application described in this document.
- **Decision_Module**: The dedicated, independently testable backend component that selects one cooking strategy using Strands Decider 2B. Implemented at `backend/app/decision_patterns/cooking_strategy.py`.
- **Decider_2B**: Strands Decider 2B, the decision model that selects a cooking strategy from the allowed options.
- **Strands_Agent**: The Strands Agents SDK agent that wires the Decision_Module to the strategy tools in the execution path.
- **Strategy**: One of exactly three cooking approaches: `QUICK_RECIPE`, `LEFTOVER_REMIX`, or `NO_COOK_SNACK`.
- **Strategy_Tool**: One of three separate, typed backend tools that generate a recipe for a specific Strategy: `quick_recipe`, `leftover_remix`, `no_cook_snack`.
- **Bedrock_Generator**: The backend component that calls Amazon Bedrock to produce recipe content after a Strategy is selected.
- **Recipe_Schema**: The structured, Pydantic-validated output schema for a generated recipe.
- **Decision_Source**: The origin of the selected Strategy, either `decider_2b` (genuine Decider 2B inference) or `fallback` (deterministic fallback after inference failure).
- **Fallback_Strategy**: A deterministic Strategy choice produced only when Decider_2B inference fails; explicitly labeled and never counted as successful Decider_2B inference.
- **Dietary_Safety_Checker**: The deterministic backend component that normalizes ingredient names and enforces allergen/dietary exclusions.
- **Backend_API**: The Python/FastAPI service exposing CraveQuest's HTTP endpoints.
- **Frontend_App**: The React + TypeScript + Vite web client.
- **Metrics_Collector**: The backend component that instruments and records decision and generation telemetry.
- **Benchmark_Script**: The reproducible script comparing Decider 2B strategy selection against a separately identified baseline.
- **Decision_Pattern_Deliverable**: The reusable decision-pattern module and documentation packaged independently of CraveQuest.
- **Mock_Mode**: An explicitly labeled local-development mode used when Decider_2B integration is unavailable in the dev environment.
- **Request_Id**: A unique identifier assigned to each recipe generation request for tracing.

## Requirements

### Requirement 1: Collect Meal-Decision Inputs

**User Story:** As a hungry user, I want to tell CraveQuest about my ingredients, time, energy, craving, equipment, budget, and dietary needs, so that I receive a recipe tailored to my real situation.

#### Acceptance Criteria

1. WHEN a user submits a recipe generation request, THE Backend_API SHALL accept a request body containing `ingredients` (list of strings), `available_time_minutes` (positive integer), `energy_level` (one of `low`, `medium`, `high`), `craving` (string), `cooking_preference` (one of `cook`, `no_cook`, `surprise`), `equipment` (list of strings), `budget` (one of `low`, `medium`, `flexible`), `dietary_preferences` (list of strings), `allergens_to_avoid` (list of strings), and `servings` (positive integer).
2. IF a required input field is missing, THEN THE Backend_API SHALL reject the request with a 422 validation error that identifies the missing field.
3. IF `energy_level`, `cooking_preference`, or `budget` contains a value outside its defined enumeration, THEN THE Backend_API SHALL reject the request with a 422 validation error that identifies the invalid field.
4. IF `available_time_minutes` is less than 1 or greater than a configurable maximum available time (default 1440 minutes), OR `servings` is less than 1 or greater than a configurable maximum servings count (default 20), THEN THE Backend_API SHALL reject the request with a 422 validation error that identifies the out-of-range field.
5. IF the `ingredients` list is empty, THEN THE Backend_API SHALL reject the request with a 422 validation error that identifies the `ingredients` field.
6. IF any input string exceeds a configurable maximum string length (default 200 characters) or any input list exceeds a configurable maximum list element count (default 50 elements), THEN THE Backend_API SHALL reject the request with a 422 validation error that identifies the offending field.
7. IF a request body exceeds a configurable maximum request body size (default 64 KB), THEN THE Backend_API SHALL reject the request with a 413 error.

### Requirement 2: Select Exactly One Cooking Strategy via Decider 2B

**User Story:** As a product owner, I want the app to make a real, observable cooking-strategy decision using Strands Decider 2B, so that CraveQuest demonstrates a genuine tool-selection decision pattern rather than a hidden rule tree.

#### Acceptance Criteria

1. WHEN the Decision_Module receives validated meal-decision inputs, THE Decision_Module SHALL request a Strategy selection from Decider_2B over the three allowed options `QUICK_RECIPE`, `LEFTOVER_REMIX`, and `NO_COOK_SNACK`.
2. THE Decision_Module SHALL select exactly one allowed Strategy option per request.
3. WHEN Decider_2B returns a selection, THE Decision_Module SHALL validate that the returned value is one of the three allowed Strategy options.
4. IF Decider_2B returns a value that is not one of the three allowed Strategy options, THEN THE Decision_Module SHALL treat the inference as failed and SHALL apply the Fallback_Strategy.
5. WHERE the Decider_2B documented API exposes a confidence value, THE Decision_Module SHALL capture the decision confidence as a value between 0.0 and 1.0 inclusive.
6. THE Decision_Module SHALL measure and record the decision latency as a non-negative integer number of milliseconds for each Strategy selection.
7. WHEN a Strategy is selected under normal operation, THE Decision_Module SHALL set the Decision_Source to `decider_2b`.
8. THE Decision_Module SHALL derive the normal Strategy choice from Decider_2B inference and SHALL NOT derive the normal Strategy choice from a hardcoded conditional tree or a plain Bedrock language-model call.

### Requirement 3: Apply Configurable Confidence Threshold

**User Story:** As an operator, I want a configurable confidence threshold for the decision, so that low-confidence selections can be handled consistently when confidence is available.

#### Acceptance Criteria

1. WHERE Decider_2B exposes a confidence value, THE Decision_Module SHALL compare the captured confidence against a configurable confidence threshold bounded to the range 0.0 to 1.0 inclusive with a default of 0.5.
2. THE Decision_Module SHALL read the confidence threshold from configuration rather than from a hardcoded literal in execution logic.
3. IF the configured confidence threshold is outside the range 0.0 to 1.0 inclusive, THEN THE Decision_Module SHALL fail startup validation and SHALL report the out-of-range threshold.
4. WHERE a confidence value is available AND the confidence is strictly below the configured threshold, THE Decision_Module SHALL record that the threshold condition was met in the decision metadata.

### Requirement 4: Handle Decision Failure with Deterministic Fallback

**User Story:** As a user, I want CraveQuest to still return a recipe when the decision model is unavailable, so that a failure does not leave me without a result, while keeping the fallback clearly distinguished from a genuine decision.

#### Acceptance Criteria

1. IF Decider_2B inference raises a runtime error or does not complete within a configurable decision timeout (default 5000 milliseconds) read from configuration, THEN THE Decision_Module SHALL apply a deterministic Fallback_Strategy.
2. WHEN a Fallback_Strategy is applied, THE Decision_Module SHALL set the Decision_Source to `fallback`.
3. WHEN a Fallback_Strategy is applied, THE Metrics_Collector SHALL record the event as a decision failure and SHALL NOT count the event as a successful Decider_2B inference.
4. WHEN a Fallback_Strategy is applied, THE Backend_API SHALL include the `fallback` Decision_Source in the response so the result is explicitly marked.
5. THE Decision_Module SHALL apply the Fallback_Strategy only when Decider_2B inference fails and SHALL NOT apply the Fallback_Strategy during successful inference.

### Requirement 5: Route to the Selected Strategy Tool

**User Story:** As a developer, I want the selected strategy to determine which single strategy tool runs, so that the decision has an observable effect on the execution path.

#### Acceptance Criteria

1. WHEN a Strategy is selected, THE Strands_Agent SHALL route execution to exactly one Strategy_Tool that corresponds to the selected Strategy.
2. WHEN a single request is processed, THE Strands_Agent SHALL execute exactly one Strategy_Tool and SHALL NOT execute zero or more than one Strategy_Tool.
3. WHEN the selected Strategy is `QUICK_RECIPE`, THE Strands_Agent SHALL invoke the `quick_recipe` Strategy_Tool.
4. WHEN the selected Strategy is `LEFTOVER_REMIX`, THE Strands_Agent SHALL invoke the `leftover_remix` Strategy_Tool.
5. WHEN the selected Strategy is `NO_COOK_SNACK`, THE Strands_Agent SHALL invoke the `no_cook_snack` Strategy_Tool.
6. THE Backend_API SHALL include the selected Strategy and the decision metadata in the recipe generation response.

### Requirement 6: Quick Recipe Strategy Tool

**User Story:** As a user with enough time and energy, I want a straightforward cooked recipe, so that I can make a proper meal that fits my constraints.

#### Acceptance Criteria

1. WHEN the `quick_recipe` Strategy_Tool runs, THE `quick_recipe` Strategy_Tool SHALL produce a recipe whose combined `prep_time_minutes` and `cook_time_minutes` do not exceed the request `available_time_minutes`, whose ingredients and instructions exclude all request dietary preferences and allergens, and whose instructions require only equipment listed in the request.
2. THE `quick_recipe` Strategy_Tool SHALL produce a recipe whose `servings` equals the request `servings` and in which every ingredient has a non-empty quantity.
3. THE `quick_recipe` Strategy_Tool SHALL mark each request ingredient with `is_available` set to true and SHALL limit ingredients with `is_available` set to false to a configurable maximum additional-ingredient count (default 5).

### Requirement 7: Leftover Remix Strategy Tool

**User Story:** As a user with odds and ends on hand, I want a creative combination of what I already have, so that I minimize extra purchases and reduce waste.

#### Acceptance Criteria

1. WHEN the `leftover_remix` Strategy_Tool runs, THE `leftover_remix` Strategy_Tool SHALL mark every request ingredient with `is_available` set to true.
2. WHERE a substituted item is absent from the request ingredients, THE `leftover_remix` Strategy_Tool SHALL include the substitution only in that case and SHALL record the substitution in the `substitutions` field.
3. THE `leftover_remix` Strategy_Tool SHALL limit ingredients with `is_available` set to false to a configurable maximum additional-ingredient count (default 3).
4. THE `leftover_remix` Strategy_Tool SHALL populate the `reasoning_summary` field with non-empty text no longer than a configurable maximum length (default 600 characters) that references at least one request ingredient.
5. WHERE a creative twist using only request ingredients exists, THE `leftover_remix` Strategy_Tool SHALL record the creative twist in the `bonus_challenge` field.

### Requirement 8: No-Cook Snack Strategy Tool

**User Story:** As a low-energy user with limited time or equipment, I want a genuinely no-cook option, so that I can eat without cooking.

#### Acceptance Criteria

1. WHEN the `no_cook_snack` Strategy_Tool runs, THE `no_cook_snack` Strategy_Tool SHALL produce a recipe whose `cook_time_minutes` equals 0 and in which no instruction step requires heat.
2. THE `no_cook_snack` Strategy_Tool SHALL produce a recipe whose `prep_time_minutes` does not exceed a configurable maximum no-cook preparation time (default 10 minutes).
3. THE `no_cook_snack` Strategy_Tool SHALL produce a recipe whose instructions require only equipment listed in the request and whose ingredients and instructions exclude all request dietary preferences and allergens.

### Requirement 9: Generate Recipe Content with Amazon Bedrock

**User Story:** As a user, I want rich, generated recipe content after a strategy is chosen, so that I receive a complete and engaging recipe.

#### Acceptance Criteria

1. WHEN a Strategy_Tool runs, THE Bedrock_Generator SHALL generate the recipe content using Amazon Bedrock after the Strategy has been selected.
2. THE Bedrock_Generator SHALL supply the selected Strategy to Amazon Bedrock as a generation constraint and SHALL set the Recipe_Schema `strategy` field to the selected Strategy.
3. THE Bedrock_Generator SHALL read the Bedrock model identifier from an environment variable and SHALL NOT use a hardcoded model identifier.
4. THE Bedrock_Generator SHALL produce recipe content conforming to the Recipe_Schema containing `title`, `description`, `strategy`, `quest_title`, `quest_story`, `prep_time_minutes`, `cook_time_minutes`, `difficulty` (one of `Easy`, `Medium`, `Hard`), `servings`, `ingredients` (each with `name`, `quantity`, `is_available`), `instructions`, `substitutions`, `bonus_challenge`, and `reasoning_summary`.
5. WHEN the Bedrock_Generator validates the generated content against the Recipe_Schema using Pydantic, THE Bedrock_Generator SHALL treat content that omits any required Recipe_Schema field or that contains a `difficulty` value outside the set {`Easy`, `Medium`, `Hard`} as a validation failure.
6. THE Bedrock_Generator SHALL return a user-facing explanation no longer than a configurable maximum length (default 600 characters) and SHALL NOT expose model chain-of-thought content.

### Requirement 10: Handle Bedrock Generation Failures Safely

**User Story:** As a user, I want CraveQuest to recover from or clearly report generation problems, so that I never receive a broken or malformed recipe.

#### Acceptance Criteria

1. IF Amazon Bedrock returns malformed JSON or content that fails Recipe_Schema validation, THEN THE Bedrock_Generator SHALL reject the content and SHALL NOT return the malformed content to the Frontend_App.
2. IF Amazon Bedrock returns empty content, THEN THE Bedrock_Generator SHALL treat the response as a generation failure.
3. IF Amazon Bedrock returns a throttling response, a service error, or a timeout, THEN THE Bedrock_Generator SHALL retry the request up to a configurable maximum retry count (default 3) using exponential backoff with a configurable initial delay (default 500 milliseconds) and a configurable maximum delay (default 8000 milliseconds).
4. IF a single Amazon Bedrock attempt does not complete within a configurable per-attempt timeout (default 30000 milliseconds), THEN THE Bedrock_Generator SHALL treat the attempt as a timeout failure.
5. IF the configured retries are exhausted without a schema-valid result, THEN THE Backend_API SHALL return a safe error response that excludes stack traces, credentials, and internal prompts and that indicates a generation failure.

### Requirement 11: Health Endpoint

**User Story:** As an operator, I want a health endpoint, so that I can check service status without exposing sensitive information.

#### Acceptance Criteria

1. WHEN a client sends a GET request to `/health`, THE Backend_API SHALL respond within a configurable health response timeout (default 1000 ms) with a status payload containing only a service status indicator and a timestamp.
2. THE Backend_API SHALL NOT include secrets, credentials, internal configuration, dependency connection strings, or environment variable values in the `/health` response.

### Requirement 12: Recipe Generation Endpoint

**User Story:** As a frontend developer, I want a generate endpoint that returns the recipe plus decision metadata, so that the UI can display the result and how the decision was made.

#### Acceptance Criteria

1. WHEN a client sends a POST request to `/api/recipes/generate` whose body conforms to the required request schema, THE Backend_API SHALL respond with the generated `recipe`, the `selected_strategy`, the `decision_source` (one of `decider_2b` or `fallback`), the `decision_latency_ms`, the `generation_latency_ms`, and a `request_id`.
2. WHERE a decision confidence value is genuinely available, THE Backend_API SHALL include `decision_confidence` in the response.
3. WHERE a decision confidence value is not genuinely available, THE Backend_API SHALL omit `decision_confidence` from the response rather than reporting a placeholder value.
4. THE Backend_API SHALL NOT include stack traces, credentials, or internal prompts in the `/api/recipes/generate` response.
5. IF a POST request to `/api/recipes/generate` has a missing or malformed body or fails request schema validation, THEN THE Backend_API SHALL reject the request with an error response indicating which field is invalid, without generating a recipe.

### Requirement 13: Recipe Regeneration Endpoint

**User Story:** As a user, I want to try another recipe or a different strategy, so that I can explore alternatives when the first result does not appeal to me.

#### Acceptance Criteria

1. WHEN a client sends a POST request to `/api/recipes/regenerate` whose body conforms to the required request schema, THE Backend_API SHALL return either a new recipe for the same Strategy or a recipe for a different Strategy.
2. IF the regeneration changes the Strategy, THEN THE Backend_API SHALL change the Strategy only in response to an explicit user request for a different strategy or a new Decider_2B decision.
3. THE Backend_API SHALL NOT include stack traces, credentials, or internal prompts in the `/api/recipes/regenerate` response.
4. IF a POST request to `/api/recipes/regenerate` has a missing or malformed body or fails request schema validation, THEN THE Backend_API SHALL reject the request with an error response indicating which field is invalid, without regenerating a recipe.

### Requirement 14: Metrics Summary Endpoint

**User Story:** As a developer presenting a demo, I want a metrics summary endpoint, so that I can show decision and generation telemetry without exposing it publicly.

#### Acceptance Criteria

1. WHEN a client that presents valid metrics-access credentials sends a GET request to `/api/metrics/summary`, THE Backend_API SHALL respond with development and demo metrics limited to aggregated decision counts, Decision_Source distribution, and decision and generation latency summaries.
2. THE Backend_API SHALL protect the `/api/metrics/summary` endpoint with an access-credential check or exclude the endpoint from the public deployment.
3. IF a client without valid metrics-access credentials sends a GET request to `/api/metrics/summary` on a deployment where the endpoint is exposed, THEN THE Backend_API SHALL reject the request with an error response and SHALL NOT include any metrics data.

### Requirement 15: Configurable CORS

**User Story:** As an operator, I want CORS origins configured from the environment, so that only the intended frontend origin can call the API.

#### Acceptance Criteria

1. THE Backend_API SHALL read the allowed CORS origins from environment variables.
2. WHEN a browser request originates from a configured allowed origin, THE Backend_API SHALL permit the cross-origin request.
3. IF a browser request originates from an origin that is not in the configured allowed origins, THEN THE Backend_API SHALL deny the cross-origin request.

### Requirement 16: Landing Experience

**User Story:** As a first-time visitor, I want a welcoming landing page, so that I understand what CraveQuest does and how to start.

#### Acceptance Criteria

1. WHEN a user opens the Frontend_App, THE Frontend_App SHALL display a logo element, a tagline element, a short explanation element, and a primary call-to-action control labeled "Find my next bite".
2. WHEN the user activates the "Find my next bite" call-to-action control, THE Frontend_App SHALL navigate the user to the ingredient selection view.

### Requirement 17: Ingredient and Constraint Selection

**User Story:** As a user, I want to enter my ingredients and constraints with an easy interface, so that I can describe my situation quickly.

#### Acceptance Criteria

1. WHEN the user adds or removes an ingredient, THE Frontend_App SHALL update the selected ingredients and SHALL display each selected ingredient as a chip.
2. THE Frontend_App SHALL limit the number of selected ingredient chips to the configurable maximum list element count defined in Requirement 1 (default 50 elements).
3. THE Frontend_App SHALL provide a time selector, an energy control, a craving selector, an equipment selector, a budget selector, and a servings control.
4. THE Frontend_App SHALL display the dietary preference field and the allergen-to-avoid field prominently and SHALL allow the user to review the entered dietary preferences and allergens before submitting.
5. WHEN the user submits the selection, THE Frontend_App SHALL send the collected inputs to the `/api/recipes/generate` endpoint.

### Requirement 18: Loading State Honesty

**User Story:** As a user, I want an honest loading state, so that I am not misled about whether a real decision model ran.

#### Acceptance Criteria

1. WHILE a recipe request is in progress, THE Frontend_App SHALL display friendly loading messages.
2. IF the result is produced in Mock_Mode or via Fallback_Strategy, THEN THE Frontend_App SHALL NOT display a message that claims Decider_2B produced a genuine decision.

### Requirement 19: Recipe Result Display

**User Story:** As a user, I want a clear, playful recipe result, so that I can understand the recipe and act on it.

#### Acceptance Criteria

1. WHEN a recipe result is received, THE Frontend_App SHALL display the recipe title, the quest title, the total time, the difficulty, the servings, and a badge indicating the selected Strategy.
2. THE Frontend_App SHALL display available ingredients and additional ingredients as visually distinguished groups in the ingredient list.
3. THE Frontend_App SHALL display numbered instruction steps, the quest description, the bonus challenge, and the match explanation.
4. THE Frontend_App SHALL provide a regenerate control, a save/favorite control, and a start-cooking or mark-quest-complete control.

### Requirement 20: Frontend Error States

**User Story:** As a user, I want clear error messages, so that I know what went wrong and what to do next.

#### Acceptance Criteria

1. IF the user submits with no ingredients, THEN THE Frontend_App SHALL notify the user with a missing-ingredients error, SHALL retain the entered input, and SHALL NOT send the request.
2. IF the Backend_API returns a validation error, THEN THE Frontend_App SHALL notify the user with an invalid-input error that indicates the field at fault and SHALL retain the entered input.
3. IF the Backend_API is unavailable, THEN THE Frontend_App SHALL notify the user with a backend-unavailable error and SHALL retain the entered input.
4. IF the Backend_API reports that model access is not configured, THEN THE Frontend_App SHALL notify the user with a model-access-not-configured error and SHALL retain the entered input.
5. IF generation times out, THEN THE Frontend_App SHALL notify the user with a generation-timeout error and SHALL retain the entered input.
6. IF the Backend_API returns an invalid recipe result, THEN THE Frontend_App SHALL notify the user with an invalid-recipe error and SHALL retain the entered input.

### Requirement 21: Accessibility

**User Story:** As a user who relies on assistive technology, I want the app to be keyboard and screen-reader friendly, so that I can use CraveQuest independently.

#### Acceptance Criteria

1. THE Frontend_App SHALL make all interactive controls operable with a keyboard.
2. THE Frontend_App SHALL display a visible focus indicator on the currently focused control.
3. THE Frontend_App SHALL provide text and background color combinations that meet WCAG 2.1 AA contrast ratios.
4. THE Frontend_App SHALL display loading indicators during asynchronous operations and SHALL display empty states when no content is available.

### Requirement 22: Local Persistence of Favorites and Preferences

**User Story:** As a returning user, I want my favorites and preferences remembered in my browser, so that I do not re-enter them each visit.

#### Acceptance Criteria

1. WHEN the user saves a recipe as a favorite, THE Frontend_App SHALL store the favorite in browser local storage.
2. THE Frontend_App SHALL store lightweight user preferences in browser local storage.
3. WHEN the user returns to the Frontend_App, THE Frontend_App SHALL load saved favorites and preferences from browser local storage.
4. THE Frontend_App SHALL limit stored favorites to a configurable maximum favorites count (default 50).
5. WHEN the user returns to the Frontend_App AND browser local storage contains no saved favorites or preferences, THE Frontend_App SHALL load default empty state without error.

### Requirement 23: Deterministic Dietary Exclusion Before Generation

**User Story:** As a user with dietary restrictions, I want exclusions enforced by deterministic logic rather than by prompt text alone, so that my restrictions are reliably respected.

#### Acceptance Criteria

1. WHEN meal-decision inputs are received, THE Dietary_Safety_Checker SHALL normalize ingredient names and known aliases to a canonical form.
2. IF an ingredient name cannot be normalized to a canonical form, THEN THE Dietary_Safety_Checker SHALL fail safe by treating the ingredient as unresolved and SHALL NOT silently pass the ingredient.
3. WHEN normalization completes, THE Dietary_Safety_Checker SHALL perform a deterministic exclusion check against the user's dietary preferences and allergens before recipe generation begins.
4. THE Bedrock_Generator SHALL include all active dietary restrictions as generation constraints supplied to Amazon Bedrock.

### Requirement 24: Validate Generated Recipe Against Exclusions

**User Story:** As a user with dietary restrictions, I want generated recipes checked against my exclusions, so that an excluded ingredient never reaches me unchecked.

#### Acceptance Criteria

1. WHEN a recipe is generated, THE Dietary_Safety_Checker SHALL validate every returned ingredient and every instruction against the user's exclusions before the recipe is presented to the user.
2. IF a generated recipe contains an excluded ingredient, THEN THE Dietary_Safety_Checker SHALL reject the recipe and SHALL trigger regeneration up to a maximum of 3 regeneration attempts.
3. IF all 3 regeneration attempts fail to produce a recipe free of excluded ingredients, THEN THE Dietary_Safety_Checker SHALL fail safe by withholding the recipe and returning a safety error.
4. THE Dietary_Safety_Checker SHALL preserve the user's explicit dietary restrictions and SHALL NOT relax a restriction for the sake of recipe creativity.

### Requirement 25: Honest Safety Claims and Fail-Safe Handling

**User Story:** As a user, I want CraveQuest to be honest about the limits of its safety checks, so that I know to verify labels and am protected when ingredients are ambiguous.

#### Acceptance Criteria

1. THE Frontend_App SHALL advise users to verify product labels and SHALL NOT claim that a recipe is allergy-safe when labels, cross-contamination, or ingredient aliases cannot be verified.
2. THE Dietary_Safety_Checker SHALL NOT present unverified medical claims and SHALL limit any nutritional claims to data provided in the request.
3. IF an ingredient cannot be unambiguously classified against the user's exclusions, THEN THE Dietary_Safety_Checker SHALL regenerate under stricter constraints up to a maximum of 3 attempts, and IF all 3 attempts remain ambiguous, THEN THE Dietary_Safety_Checker SHALL request clarification from the user.
4. THE Decision_Pattern_Deliverable SHALL document the limitations of keyword-based ingredient filtering.

### Requirement 26: Security and Privacy Controls

**User Story:** As a security-conscious operator, I want least-privilege access and safe handling of inputs and secrets, so that CraveQuest minimizes exposure of credentials and personal data.

#### Acceptance Criteria

1. THE CraveQuest deployment SHALL grant each component only the IAM permissions required for its operations and SHALL NOT grant wildcard-action or administrator-equivalent permissions.
2. THE Frontend_App SHALL NOT contain AWS credentials in source, build artifacts, or configuration.
3. THE CraveQuest repository SHALL NOT contain committed `.env` files, credentials, API keys, or other secrets.
4. WHEN the Backend_API receives a request, THE Backend_API SHALL validate inputs against the documented schema and SHALL reject any request whose body exceeds a configurable maximum size (default 256 KB).
5. IF a request fails validation or exceeds the configured size limit, THEN THE Backend_API SHALL reject the request with an error response indicating the validation failure, SHALL exclude stack traces, credentials, and internal prompts from that response, and SHALL NOT persist the rejected input.
6. WHEN the Backend_API returns an error response, THE Backend_API SHALL exclude stack traces, credentials, and internal prompts from the response body.
7. WHEN the Backend_API writes logs to Amazon CloudWatch, THE Backend_API SHALL exclude complete personal profiles and complete sensitive input values unless verbose logging is explicitly enabled via a configurable flag (default disabled).
8. WHEN a request completes, THE Backend_API SHALL retain only the data required to return the response for that request and SHALL NOT persist user-provided personal data after the response is returned.

### Requirement 27: Cost Controls and Documentation

**User Story:** As a budget-conscious operator, I want generation limits and clear cost documentation, so that I can run CraveQuest without unexpected spend.

#### Acceptance Criteria

1. WHEN the Bedrock_Generator calls Amazon Bedrock, THE Bedrock_Generator SHALL apply a configurable per-call timeout (default 30 seconds) and a configurable maximum retry count (default 2 retries) with backoff between attempts.
2. IF all configured Amazon Bedrock retries are exhausted, THEN THE Bedrock_Generator SHALL stop retrying and SHALL return an error response indicating that generation failed.
3. THE Backend_API SHALL enforce a configurable per-request generation limit (default 1 recipe per request) and a configurable per-session request limit (default 20 requests per session).
4. IF a request would exceed a configured generation limit, THEN THE Backend_API SHALL reject the request with an error response indicating the limit was reached.
5. THE CraveQuest documentation SHALL include an AWS budget-monitoring checklist.
6. THE CraveQuest documentation SHALL identify each paid AWS resource, distinguish MVP-required services from optional services, and SHALL NOT state that deployment is free of charge.

### Requirement 28: AWS Deployment Architecture

**User Story:** As an operator, I want the simplest deployable AWS architecture, so that CraveQuest is straightforward to run and host.

#### Acceptance Criteria

1. THE CraveQuest deployment SHALL host the Frontend_App on AWS Amplify Hosting.
2. THE CraveQuest deployment SHALL expose the Backend_API through Amazon API Gateway.
3. WHERE AWS Lambda is compatible with the Decider_2B dependencies and runtime, THE CraveQuest deployment SHALL run the Backend_API on AWS Lambda.
4. IF AWS Lambda is not compatible with the Decider_2B dependencies and runtime, THEN THE CraveQuest deployment SHALL run the Backend_API on a container service and THE documentation SHALL state the reason for the container service choice.
5. THE CraveQuest deployment SHALL use Amazon Bedrock for recipe generation and SHALL emit logs and metrics to Amazon CloudWatch.
6. THE CraveQuest deployment SHALL define infrastructure using AWS CDK or AWS SAM.
7. WHERE server-side persistence is required by a documented feature, THE CraveQuest deployment SHALL use Amazon DynamoDB.
8. WHERE the deployment is the MVP, THE CraveQuest deployment SHALL rely on browser local storage instead of server-side persistence.
9. WHEN deployment completes, THE CraveQuest deployment SHALL expose outputs for the frontend URL and the API URL.

### Requirement 29: Deployment and Developer Documentation

**User Story:** As a new contributor, I want complete setup and deployment documentation, so that I can run, deploy, and tear down CraveQuest.

#### Acceptance Criteria

1. THE CraveQuest documentation SHALL include a deployment guide, an environment variable reference, local development instructions, credential setup instructions, Bedrock model-access instructions, deploy and teardown commands, and a troubleshooting section.

### Requirement 30: Separate Decision and Generation Instrumentation

**User Story:** As a developer evaluating the decision pattern, I want decision and generation stages instrumented separately, so that I can report accurate per-stage performance.

#### Acceptance Criteria

1. THE Metrics_Collector SHALL instrument the decision stage and the generation stage as separately identified measurements.
2. WHEN a request is processed, THE Metrics_Collector SHALL record request count, strategy selection counts, Decider latency, Bedrock latency, end-to-end latency, decision failures, fallback rate, and recipe validation failures.
3. THE Metrics_Collector SHALL report decision latency as a value distinct from total agent latency.

### Requirement 31: Reproducible Benchmark

**User Story:** As a developer, I want a reproducible benchmark comparing Decider 2B against a baseline, so that any performance claims are grounded in measurement.

#### Acceptance Criteria

1. THE Benchmark_Script SHALL compare Decider_2B strategy selection against a baseline that is named in the benchmark output, using identical inputs for both.
2. THE Benchmark_Script SHALL run a configurable sample size (default 100 samples) per compared subject.
3. THE Benchmark_Script SHALL report the sample count, the median, and the p95 for each compared measurement.
4. THE CraveQuest documentation SHALL NOT state performance improvements that are not supported by the Benchmark_Script measurements.

### Requirement 32: Demo Scenario

**User Story:** As a presenter, I want a concise demo scenario, so that I can show CraveQuest end to end within a short time limit.

#### Acceptance Criteria

1. THE CraveQuest documentation SHALL include a demo scenario whose documented steps are completable in 180 seconds or less.

### Requirement 33: Reusable Decision Pattern Deliverable

**User Story:** As an engineer on another project, I want the decision pattern packaged and documented reusably, so that I can adapt it outside CraveQuest.

#### Acceptance Criteria

1. THE Decision_Pattern_Deliverable SHALL provide a dedicated module at `backend/app/decision_patterns/cooking_strategy.py`, a `README.md`, and tests.
2. THE Decision_Pattern_Deliverable SHALL document the decision problem, the typed question, the allowed options and their meanings, the input context schema, the actual Decider_2B API and integration, the agent wiring, the option-to-tool mapping, the error and fallback handling, the test instructions, and a minimal adaptation example.
3. THE Decision_Pattern_Deliverable SHALL include the tag `#tool-selection`.
4. THE Decision_Pattern_Deliverable SHALL be installable and executable without requiring any CraveQuest-specific component.

### Requirement 34: Decider 2B Adapter and Labeled Mock Mode

**User Story:** As a local developer, I want Decider 2B isolated behind an adapter with a clearly labeled mock mode, so that I can develop the UI without the real model while never presenting mock decisions as genuine.

#### Acceptance Criteria

1. THE Decision_Module SHALL isolate Decider_2B behind an adapter exposing a documented interface.
2. WHERE Decider_2B integration is unavailable in the development environment, THE Decision_Module SHALL provide a Mock_Mode for local UI development that is enabled only by explicit configuration.
3. WHEN a decision is produced in Mock_Mode, THE Backend_API SHALL mark the decision with a mock indicator in the response and SHALL NOT present the decision as genuine Decider_2B inference.
4. THE CraveQuest implementation SHALL implement against documented interfaces and SHALL NOT invent import names, package names, model identifiers, or APIs.
