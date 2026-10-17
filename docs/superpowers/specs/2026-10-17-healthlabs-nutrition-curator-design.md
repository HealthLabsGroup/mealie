# HealthLabs Nutrition Curator Design

Date: 2026-10-17
Status: Approved by direct implementation request

## Intent

Give a limited group of nutrition editors a native Mealie screen for turning
structured recipe foods into reviewed, source-backed nutrition facts. The
workflow must use the running HealthLabs API and real database records; scripts,
mock foods, and automatic approval are outside the product flow.

## Scope

The first version is one page at `/nutrition`. It lets an editor:

- browse and search foods from their current Mealie workspace;
- switch between foods needing review, matched foods, and all foods;
- choose a source country, defaulting to Great Britain;
- search authoritative source foods by normalized name;
- compare the five core nutrient values per 100 g and source provenance;
- approve or replace a match, clear an incorrect match, and add a measured
  portion to a reviewed match.

The page discovers the current user's active HealthLabs capabilities through a
new authenticated self-only read endpoint. The navigation link and route are
available only when `nutrition:write` is active. The backend remains the final
authorization boundary for every mutation.

## Non-goals

- No bulk or automatic matching.
- No invented, averaged, or manually typed nutrient values.
- No source import or release management UI.
- No cross-workspace food browser and no user health data.
- No new dependency or standalone frontend. One guarded migration scopes
  editor-measured portions to their workspace; ambiguous legacy ownership and
  lossy downgrade stop with an actionable error. Source-managed portions remain
  global catalogue facts.

## Interaction

The workbench uses the existing Mealie shell and Vuetify components.

```text
+-------------------------------------------------------------------+
| Nutrition curator                Country [GB]   Search foods [...] |
+--------------------------+----------------------------------------+
| Needs review  Matched All| Selected food                          |
|                          | Current source / portions (when set)   |
| Olive oil         Review |                                        |
| Egg               Unknown| Find a source [olive oil_______] [Find]|
| Carrot            Unknown|                                        |
|                          | CoFID · GB · 2021                       |
|                          | Olive oil                               |
|                          | kcal protein fibre carbs fat            |
|                          |                         [Use this source]|
|                          |                                        |
|                          | Measured portions                       |
|                          | description amount unit grams    [Add]  |
+--------------------------+----------------------------------------+
```

Selecting a food seeds source search with its name. Approving a result updates
the selected food in place and records the fixed audit reason “Nutrition
curator source review”. Adding or clearing data uses equally specific fixed
reasons. Success keeps the editor on the same food; errors leave the current
selection and input intact.

## Visual direction

- **Colour:** existing Mealie tokens only: primary `#E58325`, accent `#007A99`,
  success `#43A047`, error `#EF5350`, and theme surface/background variables.
- **Type:** the existing application typeface and scale; nutrient numbers use
  tabular figures for scanability.
- **Layout:** a quiet two-column workbench at desktop widths and a single
  stacked flow on small screens. The candidate nutrient strip is the one strong
  information element; cards, shadows, and decoration stay restrained.
- **Accessibility:** every control has a visible label, selected states include
  text as well as colour, keyboard focus stays visible, and live loading/empty
  states explain the next action.

This deliberately avoids a new visual identity: the curator is an operational
Mealie tool, not a separate HealthLabs application.

## Security and data boundaries

- `GET /api/healthlabs/v1/capabilities` returns only the authenticated user's
  active capability names, sorted deterministically.
- Food list reads remain scoped to the authenticated user's Mealie group.
- Editor-measured portions carry that group ID and are visible and calculated
  only in that group; source-managed portions remain global.
- Source search returns public licensed facts for the chosen country plus
  `OTHER`; it does not approve a match.
- Match and portion writes retain `nutrition:write`, idempotency, access-reason,
  source-country validation, and immutable audit logging.
- Revoking `nutrition:write` hides the link on the next capability refresh and
  immediately blocks writes on the server.

## Acceptance criteria

1. A user without `nutrition:write` cannot enter `/nutrition` and never sees its
   navigation link.
2. An editor can load real foods, select one, search real source records, and
   approve a result without a script.
3. Candidate cards expose nullable nutrient values and exact source provenance;
   missing values display as unavailable rather than zero.
4. A matched food exposes its current portions and accepts a positive measured
   portion through the existing audited API.
5. Clearing or replacing a match refreshes the same food without leaking data
   from another workspace.
6. Desktop and mobile layouts remain usable with keyboard and screen-reader
   labels.
