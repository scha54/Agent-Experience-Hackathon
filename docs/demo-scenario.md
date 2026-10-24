# CraveQuest Demo Scenario

A scripted, presenter-ready walkthrough that shows CraveQuest end to end in **180 seconds or less**.
It covers the exact inputs the presenter types, the honest loading state, and the decision metadata the
audience sees that distinguishes a **genuine Decider_2B decision** from a **fallback** or **mock** result.

- Validates: Requirement 32.1 (demo completable in 180 seconds or less)
- Honesty requirements referenced: Requirement 18 (Loading State Honesty), Requirements 12.2/12.3, 34.3
- Response shape referenced: `GenerateResponse` in `design.md`

> Total scripted time below sums to **~175 seconds**, leaving a small buffer. Per-step seconds are
> approximate and annotated so a presenter can keep pace.

---

## Before you start (not counted in the 180s)

Set these up ahead of the demo so the clock only covers the live walkthrough:

1. Backend running and reachable; frontend running against it (`npm run dev` in `frontend/`).
2. Browser open at the Landing view, zoomed so the whole viewport is readable from the back of the room.
3. Decide which decision path you want to showcase and configure the environment accordingly:
   - **Genuine Decider_2B** — `DECIDER_MODE` set to the real adapter with a reachable Decider_2B server
     (`STRANDS_DECIDER_API_BASE`, checkpoint configured). Produces `decision_source: "decider_2b"`.
   - **Mock** — `DECIDER_MODE=mock`. Produces `is_mock: true` with no `decision_confidence`.
   - **Fallback** — leave the real adapter configured but point it at an unreachable/timing-out Decider_2B
     server so inference fails. Produces `decision_source: "fallback"` with no `decision_confidence`.
4. Clear browser local storage if you want a clean favorites/preferences state.

The primary script below demonstrates the **genuine Decider_2B** path. Variant endings for the mock and
fallback paths are provided so you can honestly narrate whichever path the environment actually produced.

---

## The exact inputs (what the presenter types)

Use these values verbatim so the demo is reproducible and the decision is predictable. This input set is a
clear "I can cook and have time" situation, which steers a genuine decision toward `QUICK_RECIPE`.

| Field (Selection view control) | Request field (`GenerateRequest`) | Value to enter |
| --- | --- | --- |
| Ingredients (chips) | `ingredients` | `eggs`, `spinach`, `cheddar cheese`, `onion`, `butter` |
| Available time | `available_time_minutes` | `25` |
| Energy level | `energy_level` | `medium` |
| Craving | `craving` | `something warm and savory` |
| Cooking preference | `cooking_preference` | `cook` |
| Equipment | `equipment` | `stovetop`, `nonstick pan`, `spatula` |
| Budget | `budget` | `medium` |
| Dietary preferences | `dietary_preferences` | `vegetarian` |
| Allergens to avoid | `allergens_to_avoid` | `peanuts`, `shellfish` |
| Servings | `servings` | `2` |

Why this set: `cooking_preference = cook`, `energy_level = medium`, and `available_time_minutes = 25` give the
decider a situation that fits a short cooked meal, so a genuine decision is expected to select
`QUICK_RECIPE`. The dietary preference (`vegetarian`) and allergens (`peanuts`, `shellfish`) are entered so
the audience sees the prominent dietary/allergen fields (Req 17.4) and the safety messaging on the result.

---

## Scripted timeline (~175 seconds)

### Step 1 — Landing (0:00–0:15, ~15s)

- Show the Landing view: logo, tagline, short explanation, and the primary CTA labeled exactly
  **"Find my next bite"**.
- Say: "CraveQuest decides *how* you should cook tonight, then generates the recipe for that path."
- Click **Find my next bite** to go to the Selection view.

### Step 2 — Enter ingredients and constraints (0:15–1:10, ~55s)

- Add the five ingredient chips: `eggs`, `spinach`, `cheddar cheese`, `onion`, `butter`.
  Narrate that chips are capped at the max list count (50).
- Set **Available time** to `25`, **Energy** to `medium`, **Craving** to `something warm and savory`,
  **Cooking preference** to `cook`, **Budget** to `medium`, **Servings** to `2`.
- Add **Equipment**: `stovetop`, `nonstick pan`, `spatula`.
- Point at the prominent **Dietary preferences** and **Allergens to avoid** fields; enter `vegetarian`,
  and `peanuts` + `shellfish`. Say: "These are enforced deterministically before and after generation, not by
  prompt text alone." (Review-before-submit satisfies Req 17.4.)

### Step 3 — Submit and watch the honest loading state (1:10–1:35, ~25s)

- Click submit. The app sends the inputs to `POST /api/recipes/generate` and shows the **Loading view**.
- The Loading view shows friendly, honest copy (Req 18.1):
  - Heading: "Finding your next bite."
  - Status line: "Preparing something delicious…"
  - An honesty line whose wording depends on the real decision path (Req 18.2):
    - Genuine path: "The live decision model selected a cooking path for this quest."
    - Mock **or** fallback path: "This quest is using a backup cooking path. We will keep that distinction
      clear when your recipe arrives."
    - Unknown/not-yet-known: "We will tell you whether the live decision model or a backup path shaped your
      recipe."
- Say: "Notice the loading state never claims a genuine model decision unless one actually happened. In mock
  or fallback runs it says so up front." This is the honesty guarantee from Requirement 18 and 34.3.

### Step 4 — Result view and the recipe (1:35–2:30, ~55s)

- The **Result view** renders the recipe from the `recipe` field of `GenerateResponse`:
  - Title, quest title, description.
  - A **strategy badge** reflecting `recipe.strategy` (e.g. "Quick Recipe" for `QUICK_RECIPE`).
  - Meta row: **Total time**, **Difficulty**, **Servings** (`2`).
  - Two ingredient groups — "From your kitchen" (`is_available: true`) vs. "A few extras to grab"
    (`is_available: false`).
  - Numbered **Steps**, the **quest story**, an optional **Bonus challenge**, and the **Why this match**
    explanation (`reasoning_summary`).
- Call out the safety note shown on the result: CraveQuest cannot guarantee a recipe is allergy-safe; always
  verify product labels (Req 25.1).

### Step 5 — Call out the decision metadata (2:30–2:55, ~25s)

This is the key "genuine vs fallback/mock" moment. Tie the on-screen result to the `GenerateResponse` fields.

- `selected_strategy` — the Strategy the decision chose (shown as the strategy badge).
- `decision_source` — **`decider_2b`** for a genuine Decider_2B decision, or **`fallback`** for the
  deterministic fallback after inference failure.
- `decision_confidence` — present **only** when the decision is a genuine Decider_2B inference with a real
  confidence value (0.0–1.0). It is **omitted entirely** on fallback and in mock mode — never shown as a
  placeholder (Req 12.2/12.3).
- `is_mock` — **`true`** only when the result came from the explicitly labeled Mock_Mode; **`false`**
  otherwise (Req 34.3).
- `decision_latency_ms` and `generation_latency_ms` — the two stages reported separately.

Narrate the mapping the audience can verify (the Loading view honesty line already previewed this, and the
same distinction is carried in the response):

- **Genuine:** `decision_source: "decider_2b"`, `is_mock: false`, and a present `decision_confidence`
  (for example `0.92`). The loading honesty line said the live model chose the path.
- **Fallback:** `decision_source: "fallback"`, `is_mock: false`, and **no** `decision_confidence`. The loading
  line said a backup cooking path was used.
- **Mock:** `is_mock: true`, **no** `decision_confidence`, and the loading line said a backup path was used.
  Mock output is never presented as genuine inference.

> Tip: To show the raw metadata to a technical audience, open the browser devtools Network tab and expand the
> `generate` response body, or hit `GET /api/metrics/summary` (with metrics credentials) to show
> aggregated decision counts and source distribution. Keep this inside the remaining time budget.

### Step 6 — Wrap: regenerate / save (2:55–2:55+, remaining buffer)

- Point to the **Regenerate recipe**, **Save to favorites**, and **Start cooking** controls.
- Optionally click **Save to favorites** to show local persistence, then stop. Close within the 180s window.

---

## Honesty checklist (what the audience must be able to trust)

- [ ] The Loading view never claims a genuine Decider_2B decision for a mock or fallback result (Req 18.2).
- [ ] `decision_source` is shown truthfully as `decider_2b` or `fallback`.
- [ ] `decision_confidence` appears only for a genuine Decider_2B decision; it is absent on fallback and mock
      (Req 12.2/12.3), never a placeholder.
- [ ] `is_mock` is `true` only in explicitly configured Mock_Mode (Req 34.3).
- [ ] The result's allergy-safety note is visible (Req 25.1).

---

## Timing summary

| Step | Window | Approx. duration |
| --- | --- | --- |
| 1. Landing | 0:00–0:15 | ~15s |
| 2. Enter inputs | 0:15–1:10 | ~55s |
| 3. Submit + honest loading | 1:10–1:35 | ~25s |
| 4. Result view | 1:35–2:30 | ~55s |
| 5. Decision metadata call-out | 2:30–2:55 | ~25s |
| 6. Wrap (regenerate/save) | 2:55+ | buffer |
| **Total** | | **~175s (≤ 180s)** |

---

## Variant endings (narrate whichever path actually ran)

Keep the same inputs and timeline; only Step 3's honesty line and Step 5's metadata change.

- **Genuine Decider_2B run** — `decision_source: "decider_2b"`, `is_mock: false`,
  `decision_confidence` present (e.g. `0.92`). Loading line: "The live decision model selected a cooking path
  for this quest."
- **Fallback run** (Decider_2B unreachable or times out past `DECISION_TIMEOUT_MS`) —
  `decision_source: "fallback"`, `is_mock: false`, no `decision_confidence`. Loading line: "This quest is
  using a backup cooking path…". Explain the deterministic fallback rule picked the strategy because the model
  call failed — this is counted as a decision failure, not a successful inference.
- **Mock run** (`DECIDER_MODE=mock`) — `is_mock: true`, no `decision_confidence`. Loading line: "This quest is
  using a backup cooking path…". State clearly that this is the labeled local Mock_Mode, not a genuine
  decision.
