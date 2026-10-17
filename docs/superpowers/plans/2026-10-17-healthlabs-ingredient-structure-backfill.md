# HealthLabs Ingredient Structure Backfill Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Safely convert legacy free-text recipe ingredient rows into structured Mealie quantity, unit, and food references so existing source-backed nutrition conversions can calculate more recipes.

**Architecture:** Add one operator-only backfill script that reuses Mealie's brute ingredient parser with an exact, ambiguity-rejecting matcher. It changes only plain rows whose positive quantity, unit, and food all resolve to existing group-scoped records; dry-run is the default, source text is retained, and existing nutrition calculation remains unchanged.

**Tech Stack:** Python 3.14, SQLAlchemy, Mealie ingredient parser, pytest

**Spec:** `docs/healthlabs/nutrition-service-v1.md`

## Global Constraints

- Missing or ambiguous values stay unknown; never coerce them to zero.
- Never create a food, unit, nutrient value, source match, or gram conversion from free text.
- Exact names and curated aliases are allowed; fuzzy matches and ambiguous aliases are rejected.
- Preserve `original_text`, recipe ownership, substitutions, references, and current nutrition provenance.
- The command is aggregate-only and dry-run by default; database writes require `--apply`.

## Review Focus

- Two foods sharing an exact alias leave the ingredient untouched.
- A near/fuzzy food or unit name leaves the ingredient untouched.
- A food without a positive quantity and exact unit leaves the ingredient untouched instead of becoming zero nutrition.
- A second applied run changes no already-structured row.
- A successful structure update uses the existing source-backed portion path without persisting an estimated conversion.

---

### Task 1: Add the exact structure backfill

**Files:**
- Create: `scripts/healthlabs/backfill-ingredient-structure.py`
- Modify: `tests/integration_tests/healthlabs/test_nutrition_backfill.py`
- Modify: `docs/healthlabs/nutrition-sources.md`

**Interfaces:**
- Produces: `backfill_ingredient_structure(session: Session, *, apply: bool, batch_size: int = 500) -> dict[str, int | bool]`.
- Reuses: Mealie `BruteForceParser`, group-scoped ingredient foods/units, and current `NutritionService` conversion behavior.

- [ ] **Step 1: Write failing integration tests.** Add `test_structure_backfill_requires_exact_complete_parse` for exact success, ambiguous alias, fuzzy name, missing quantity/unit, dry-run, source text preservation, and idempotency. Add `test_structure_backfill_enables_source_portion_calculation` to prove a reviewed food plus an imported source portion produces grams and totals after structuring.
- [ ] **Step 2: Run the focused tests.** Run `task py:test -- tests/integration_tests/healthlabs/test_nutrition_backfill.py -q`; expect failures because the new script does not exist.
- [ ] **Step 3: Implement the minimal script.** Use keyset batches, cache one exact matcher/parser per group, reject collisions, update only plain free-text rows with positive quantity plus exact existing unit and food, preserve text and notes, default to dry-run, and emit aggregate counts only.
- [ ] **Step 4: Document the command.** Add dry-run and `--apply` examples plus the no-fuzzy/no-created-conversion safety boundary to `docs/healthlabs/nutrition-sources.md`.
- [ ] **Step 5: Verify.** Run `task py:test -- tests/integration_tests/healthlabs/test_nutrition_backfill.py tests/unit_tests/healthlabs/test_nutrition_calculation.py -q`, then `task py:lint -- scripts/healthlabs/backfill-ingredient-structure.py tests/integration_tests/healthlabs/test_nutrition_backfill.py`; expect PASS.
- [ ] **Step 6: Commit.** Commit as `feat(healthlabs): structure exact recipe ingredients`.

## Final verification

- [ ] Run the complete backend test suite.
- [ ] Run the script in dry-run mode against a restored DEV database and record aggregate counts only.
- [ ] Review the whole branch for tenant leakage, data loss, fuzzy acceptance, and invented conversions.
