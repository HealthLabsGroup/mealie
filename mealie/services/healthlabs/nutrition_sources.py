from __future__ import annotations

import csv
import hashlib
import json
import re
from collections.abc import Iterable, Iterator, Mapping
from pathlib import Path
from typing import Any, TypedDict

from mealie.schema.healthlabs.nutrition_source import NutrientValue, SourceFoodRow, SourcePortion

CORE_UNITS = {
    "kcal": "kcal",
    "protein_g": "g",
    "fibre_g": "g",
    "carbs_g": "g",
    "fat_g": "g",
    "water_ml": "ml",
}

USDA_NUTRIENT_IDS = {
    1008: "kcal",
    2047: "kcal",
    2048: "kcal",
    1003: "protein_g",
    1079: "fibre_g",
    1005: "carbs_g",
    1004: "fat_g",
    1051: "water_ml",
}


class CsvSourceConfig(TypedDict):
    id: tuple[str, ...]
    name: tuple[str, ...]
    nutrients: dict[str, str]


CSV_FIELDS: dict[str, CsvSourceConfig] = {
    "cofid": {
        "id": ("Food Code", "Food code"),
        "name": ("Food Name", "Food name"),
        "nutrients": {
            "Energy (kcal)": "kcal",
            "Protein (g)": "protein_g",
            "AOAC fibre (g)": "fibre_g",
            "Fibre (g)": "fibre_g",
            "Carbohydrate (g)": "carbs_g",
            "Fat (g)": "fat_g",
            "Water (g)": "water_ml",
        },
    },
    "ciqual": {
        "id": ("alim_code",),
        "name": ("alim_nom_fr",),
        "nutrients": {
            "Energie, Règlement UE N° 1169/2011 (kcal/100 g)": "kcal",
            "Protéines, N x facteur de Jones (g/100 g)": "protein_g",
            "Fibres alimentaires (g/100 g)": "fibre_g",
            "Glucides (g/100 g)": "carbs_g",
            "Lipides (g/100 g)": "fat_g",
            "Eau (g/100 g)": "water_ml",
        },
    },
    "bls": {
        "id": ("BLS_CODE", "SBLS"),
        "name": ("NAME", "ST"),
        "nutrients": {
            "GCAL": "kcal",
            "EWP": "protein_g",
            "ZELL": "fibre_g",
            "KHC": "carbs_g",
            "FC": "fat_g",
            "WASS": "water_ml",
        },
    },
}


def verify_source_checksum(path: Path, expected_sha256: str) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    actual = digest.hexdigest()
    if actual != expected_sha256:
        raise ValueError(f"source checksum mismatch: expected {expected_sha256}, got {actual}")
    return actual


def parse_source_file(provider: str, path: Path) -> Iterable[SourceFoodRow]:
    provider = provider.lower()
    if provider == "usda":
        return _parse_usda(path)
    if provider in CSV_FIELDS:
        return _parse_csv(provider, path)
    raise ValueError(f"unsupported nutrition source provider: {provider}")


def _number(raw: Any) -> float | None:
    if isinstance(raw, int | float):
        value = float(raw)
        return value if value >= 0 else None
    if not isinstance(raw, str):
        return None
    normalized = raw.strip().replace("\u00a0", "").replace(",", ".")
    if not normalized or normalized[0] in "<-" or normalized.casefold() in {"n/a", "na", "trace", "traces"}:
        return None
    try:
        value = float(normalized)
    except ValueError:
        return None
    return value if value >= 0 else None


def _preparation_state(name: str) -> str | None:
    normalized = name.casefold()
    if any(word in normalized for word in ("cooked", "boiled", "cuite", "cuit", "gegart", "gekocht")):
        return "cooked"
    if any(word in normalized for word in ("raw", "crue", "cru", "roh")):
        return "raw"
    if any(word in normalized for word in ("dried", "dry", "séch", "getrocknet")):
        return "dried"
    return None


def _empty_core() -> dict[str, NutrientValue]:
    return {code: NutrientValue(amount=None, unit=unit) for code, unit in CORE_UNITS.items()}


def _parse_usda(path: Path) -> Iterator[SourceFoodRow]:
    document = json.loads(path.read_text(encoding="utf-8-sig"))
    if isinstance(document, list):
        foods = document
    else:
        foods = next(
            (document[key] for key in ("FoundationFoods", "SurveyFoods", "SRLegacyFoods", "foods") if key in document),
            [],
        )

    for source in foods:
        nutrients = _empty_core()
        raw: dict[str, Any] = {}
        for entry in source.get("foodNutrients", []):
            detail = entry.get("nutrient", entry)
            name = str(detail.get("name") or detail.get("nutrientName") or "").strip()
            unit = str(detail.get("unitName") or entry.get("unitName") or "").strip()
            amount = _number(entry.get("amount", entry.get("value")))
            if name:
                raw[name] = {"amount": amount, "unit": unit}
            nutrient_id = detail.get("id") or detail.get("number")
            try:
                code = USDA_NUTRIENT_IDS.get(int(nutrient_id))
            except TypeError, ValueError:
                code = None
            if code and (code != "kcal" or unit.casefold() == "kcal"):
                nutrients[code] = NutrientValue(amount=amount, unit=CORE_UNITS[code], source_name=name)

        portions = []
        for portion in source.get("foodPortions", []):
            amount = _number(portion.get("amount"))
            grams = _number(portion.get("gramWeight"))
            measure = portion.get("measureUnit") or {}
            unit = str(measure.get("name") or portion.get("modifier") or "portion").strip()
            description = str(portion.get("modifier") or unit).strip()
            if amount and grams and unit and description:
                portions.append(SourcePortion(description=description, amount=amount, unit=unit, grams=grams))

        name = str(source.get("description") or "").strip()
        yield SourceFoodRow(
            source_food_id=str(source.get("fdcId") or "").strip(),
            name=name,
            preparation_state=_preparation_state(name),
            nutrients_per_100g=nutrients,
            raw_nutrients=raw,
            portions=portions,
        )


def _first(row: Mapping[str, str | None], names: tuple[str, ...]) -> str:
    return next((str(row[name]).strip() for name in names if row.get(name)), "")


def _unit_from_header(header: str, code: str | None = None) -> str:
    match = re.search(r"\((kcal|kJ|µg|ug|mg|g|ml)(?:/100\s*g)?\)", header, flags=re.IGNORECASE)
    if match:
        return match.group(1)
    return CORE_UNITS.get(code or "", "source")


def _parse_csv(provider: str, path: Path) -> Iterator[SourceFoodRow]:
    config = CSV_FIELDS[provider]
    with path.open(encoding="utf-8-sig", newline="") as source:
        sample = source.read(4096)
        source.seek(0)
        dialect = csv.Sniffer().sniff(sample, delimiters=",;")
        for row in csv.DictReader(source, dialect=dialect):
            nutrients = _empty_core()
            nutrient_fields: dict[str, str] = config["nutrients"]
            for header, code in nutrient_fields.items():
                if header in row:
                    nutrients[code] = NutrientValue(
                        amount=_number(row[header]),
                        unit=CORE_UNITS[code],
                        source_name=header,
                    )

            excluded = {*config["id"], *config["name"]}
            raw = {
                header: {"amount": _number(value), "unit": _unit_from_header(header)}
                for header, value in row.items()
                if header not in excluded and value not in (None, "")
            }
            name = _first(row, config["name"])
            yield SourceFoodRow(
                source_food_id=_first(row, config["id"]),
                name=name,
                preparation_state=_preparation_state(name),
                nutrients_per_100g=nutrients,
                raw_nutrients=raw,
            )
