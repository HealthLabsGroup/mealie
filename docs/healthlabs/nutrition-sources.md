# HealthLabs nutrition sources

HealthLabs imports only local, checksum-pinned source files. The importer never downloads a URL, so a mutable upstream file cannot change an existing release silently.

## Approved source releases

| Provider | Pinned release | Country | Source | Licence and required attribution |
|---|---|---|---|---|
| `usda` | FoodData Central Foundation Foods 04/2026 | `OTHER` | [USDA downloads](https://fdc.nal.usda.gov/download-datasets/) | [CC0 1.0](https://fdc.nal.usda.gov/api-guide/); attribute “U.S. Department of Agriculture, Agricultural Research Service. FoodData Central.” |
| `cofid` | CoFID 2021 | `GB` | [GOV.UK release](https://www.gov.uk/government/publications/composition-of-foods-integrated-dataset-cofid) | [Open Government Licence v3.0](https://www.nationalarchives.gov.uk/doc/open-government-licence/version/3/); attribute Public Health England and McCance and Widdowson’s CoFID 2021. |
| `ciqual` | ANSES-CIQUAL 2025 | `FR` | [ANSES release](https://ciqual.anses.fr/cms/en/2025-anses-ciqual-table) | [Licence Ouverte](https://www.etalab.gouv.fr/licence-ouverte-open-licence/); attribute “Anses. 2025. Table de composition nutritionnelle des aliments Ciqual 2025”, DOI `10.57745/RDMHWY`. |
| `bls` | Bundeslebensmittelschlüssel 4.0 (2025) | `DE` | [BLS download](https://blsdb.prod.se.ble.de/bls) | [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/); attribute Max Rubner-Institut (2025), DOI `10.25826/Data20251217-134202-0`. |

Poland is blocked: the NIZP PZH full database is supplied under a purchased licence agreement, so it is not imported until HealthLabs records redistribution-compatible terms. Ukraine has no verified national open food-composition release in this policy; Ukrainian aliases may reference an approved source while retaining that source’s country and provenance.

## Accepted files

- USDA: the unzipped FoodData Central Foundation/FNDDS JSON document.
- CoFID: UTF-8 CSV exported from the published workbook, preserving the published column headers.
- CIQUAL: the official semicolon-delimited CSV.
- BLS: UTF-8 semicolon-delimited CSV exported from the published workbook, preserving BLS component codes.

Values such as `<0.5`, `trace`, blank, or `-` import as unknown (`null`), never zero. Original nutrient columns are retained under `_raw`; the canonical calculation keys are `kcal`, `protein_g`, `fibre_g`, `carbs_g`, `fat_g`, and `water_ml`.

## Manifest and import

Compute the checksum from the exact unzipped JSON or CSV passed to the importer:

```sh
shasum -a 256 /absolute/path/source.csv
```

Create a manifest whose version and URLs match the table above:

```json
{
  "provider": "cofid",
  "version": "2021",
  "country": "GB",
  "sourceUrl": "https://www.gov.uk/government/publications/composition-of-foods-integrated-dataset-cofid",
  "licenseUrl": "https://www.nationalarchives.gov.uk/doc/open-government-licence/version/3/",
  "attribution": "Public Health England; McCance and Widdowson's CoFID 2021",
  "sha256": "<64 lowercase hex characters>"
}
```

Run from the repository root:

```sh
uv run python scripts/healthlabs/import-nutrition-source.py manifest.json /absolute/path/source.csv
```

The command verifies the bytes before parsing. Reusing a provider/version/country with different bytes fails. Re-importing identical bytes updates the same source-food rows and keeps reviewed Mealie food matches attached to their existing source-food IDs.
