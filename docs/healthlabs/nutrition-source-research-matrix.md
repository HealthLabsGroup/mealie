# National nutrition source research matrix

Research snapshot: 2026-10-20

This matrix screens official national food-composition and portion datasets for
repeatable HealthLabs imports. It supplements the approved-release policy in
[`nutrition-sources.md`](nutrition-sources.md); it does not approve a source by
itself. Open Food Facts and other crowdsourced product catalogues are outside
this scope.

No source data belongs in Git. Before an import, an operator obtains the exact
official file, records its SHA-256 checksum, and keeps it outside the repository
and image build context.

Retrieval check on 2026-10-20: non-interactive requests to the linked CNF and
Fineli archives returned HTTP 403, while their official metadata, schema, and
licence pages remained available. This is not a licence rejection, but archive
retrieval and checksum reproducibility must be solved before either parser is
implemented.

## Acceptance gate

A source is importable only when all of these conditions are satisfied:

1. The national publisher provides a structured download or documented API.
2. Commercial use, transformation, and redistribution are explicitly allowed.
3. A release identifier, or a retrieval timestamp plus checksum for a rolling
   release, makes the input reproducible.
4. Food, nutrient, and measure provenance survives the import at row level.
5. Edible-part basis, nutrient definitions, measure quantity, and gram weight
   are explicit; a serving recommendation alone is not conversion evidence.

## Candidate matrix

| Priority | Dataset | Release and coverage | Portion and edible-part evidence | Provenance and nutrient definitions | Licence and redistribution | Decision |
|---|---|---|---|---|---|---|
| **P0 — import** | [USDA FoodData Central FNDDS](https://fdc.nal.usda.gov/download-datasets/) | Survey (FNDDS) 2021–2023, published 2024-10-31; 5,432 foods parsed locally | Existing parser found 16,468 positive numeric described portions; non-numeric guidance is rejected | FDC IDs, data type, release and food-portion fields are documented; keep the published description and gram weight | [CC0 1.0](https://fdc.nal.usda.gov/api-guide/); USDA requests source attribution | Already approved and supported. Import the checksum-pinned JSON before writing another parser. |
| **P1 — parser spike** | [Canadian Nutrient File (CNF)](https://www.canada.ca/en/health-canada/services/food-nutrition/healthy-eating/nutrient-data/canadian-nutrient-file-about-us.html) | 2026; 5,993 foods and up to 173 nutrients; relational CSV download | Separate measure-name/type and measure-weight-conversion files; nutrient values are per 100 g edible portion; refuse and yield are modelled separately | Food and nutrient source tables, per-value source IDs, observation counts, dates, and food last-updated dates are published in the [file structure](https://www.canada.ca/en/health-canada/services/food-nutrition/healthy-eating/nutrient-data/canadian-nutrient-file-compilation-canadian-food-composition-data-database-structure.html) | [Open Government Licence – Canada](https://open.canada.ca/en/open-government-licence-canada) permits commercial copying, modification, adaptation, and distribution with attribution | Best new candidate. Inspect the 2026 ZIP schema and measure vocabulary, then add one provider parser if it covers current gaps. |
| **P1 — parser spike** | [Fineli](https://fineli.fi/fineli/en/avoin-data) | Rolling Finnish release; current packages list 4,232 foods and 55 or 74 nutrients | `foodaddunit.csv` contains small/medium/large household measures and portion weights; values are per edible portion, with explicit exceptions for foods acquired or served unpeeled | `method` and `acquisition` describe nutrient origin; food processing/type and recipe inputs are included; the [file guide](https://fineli.fi/fineli/content/file/8) defines energy, carbohydrate, protein, and edible-part semantics | [CC BY 4.0](https://yhteistyotilat.fi/wiki08/spaces/THLKA/pages/27557991/THL+n+avoimen+datan+lisenssi+ja+vastuuvapauslauseke); attribute Finnish Institute for Health and Welfare, Fineli | Strong candidate. Because the package is rolling, pin retrieval time, exact URL, checksum, and the guide date. |
| **P2 — rights check** | [AUSNUT 2023](https://www.foodstandards.gov.au/science-data/food-nutrient-databases/ausnut/data-files) | 3,741 foods, 58 nutrients, recipes and retention factors | [9,816 measures](https://www.foodstandards.gov.au/science-data/food-nutrient-databases/ausnut/food-measures), including densities and food-specific portions | Each food and measure has derivation/source information; nutrient units, derivation, assumptions, limitations, sampling year, and source country are supplied | FSANZ's [default CC BY 4.0 terms](https://www.foodstandards.gov.au/legal-policies/copyright) allow commercial adaptation, but exclude third-party material | High technical fit. Confirm that the downloadable workbooks and every imported measure are covered before approval; retain each derivation. |
| **Hold — no derivatives** | [RIVM Portie-online](https://datacatalogus.rivm.nl/dataset.xhtml?persistentId=doi%3A10.21945%2FEE2339FE-C9DB-4C26-9CB2-D1DF6AFC79AA) | Version 2.0, 2026; Dutch units, weights, volumes, edible portions, cooking changes, and fat absorption | Purpose-built portion evidence with a DOI and versioned release | Official national publisher and documented version; intended to complement NEVO food codes | CC BY-ND 4.0; use is allowed only unchanged with source and version attribution | Do not import: normalizing names/units or deriving Mealie portions may create a prohibited adaptation. Reconsider only after written permission or legal approval. |
| **Hold — no modification** | [New Zealand FOODfiles](https://foodcomposition.co.nz/foodfiles/) | FOODfiles 2024; 2,857 foods, 87 standard and up to 434 unabridged components | `CSM.FT` contains common standard measures and density; concise tables provide common measures for most foods | Per-value provenance/source codes, recipes, retention factors, yield factors, conversion factors, and archived releases are supplied | Custom [terms of use](https://foodcomposition.co.nz/terms/) permit use and reproduction but prohibit modifying any part of the data and impose downstream terms | Do not import into the normalized catalogue without written permission or legal approval. |
| **Hold — conflicting terms** | [Swedish Food Composition Database](https://dataportal.livsmedelsverket.se/livsmedel/swagger/index.html) | API version 1.0; more than 2,400 foods and 50 nutrients; the searchable release reports version 2026-07-01 | Values are per 100 g edible part and waste percentage is available; no structured household-measure endpoint is documented | API exposes classifications, raw materials, ingredients and release metadata; value-level derivation is available in the official search UI | API says CC BY, while the [official usage page](https://www.livsmedelsverket.se/livsmedel-och-innehall/naringsamne/livsmedelsdatabasen/sok-naringsinnehall/) also says data must not be changed | Low portion value and ambiguous adaptation terms. Require written clarification before import. |
| **Hold — licence gap** | [Frida Food Data](https://www.food.dtu.dk/english/About-us/Facilities-and-infrastructure/Food-Data-database-on-nutrients-in-food) | Danish version 5.5 reports 1,381 foods and up to 150 nutrients; downloadable | Values are per 100 g edible part; no household-measure dataset is documented | Sources/references, analytical values, borrowed values, calculations, and food waste are documented | The [version 5.4 terms](https://frida.fooddata.dk/pdf/en-frida-5.4-dokumentation.pdf) require clear attribution but do not provide a standard open licence or explicit adaptation/redistribution grant | Food-composition fallback only. Do not import until commercial transformation and redistribution rights are explicit. |
| **Hold — licence/portion gap** | [Norwegian Food Composition Table API](https://www.matvaretabellen.no/en/api/) | Unversioned API with annual autumn updates; foods, nutrients, groups, LanguaL codes and sources | No portion or household-measure endpoint is documented | Separate source endpoint explains where values came from or how they were calculated; publisher requests citation | The API page requests attribution but does not state an explicit data licence | Do not import until a redistribution-compatible licence is identified; snapshot every endpoint together if later approved. |

## Recommended import order

1. Import the already-supported FNDDS 2021–2023 JSON and re-run deterministic
   matching; this adds no new parser or licence risk.
2. Inspect CNF 2026 measure names against the aggregate gap vocabulary. If it
   covers `clove`, teaspoon, tablespoon, handful, and unit-like measures, add a
   checksum-pinned `cnf` parser.
3. Run the same vocabulary check against Fineli and implement it only for
   evidence not already supplied by FNDDS/CNF.
4. Use AUSNUT only after its file-level and third-party rights are cleared; do
   not treat a recommended serving as a gram conversion.

This order follows the 2026-10-20 aggregate report: portion evidence is the
highest-value source gap, led by `cloves`, teaspoon, tablespoon, `drip`, and
handful units. Candidate ranking does not assume those words exist upstream;
the parser spike must measure exact vocabulary coverage first.

## Required provenance for an approved import

The existing source manifest remains the authority for provider, version,
country, source URL, licence URL, attribution, and SHA-256. A new importer must
also preserve, when present:

- upstream food, measure, nutrient, source/reference, and recipe identifiers;
- release/retrieval date and per-row last-updated date;
- edible-part, refuse, yield, density, amount, unit, and gram-weight semantics;
- analytical/calculated/imputed/borrowed derivation and observation count; and
- original published fields under `_raw`, without silently converting missing,
  trace, below-detection, range, or textual values to zero.

Import code, manifest metadata, checksums, licence evidence, and retrieval
instructions may be committed. Downloaded archives, extracted source rows, and
generated gap reports remain outside Git.
