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

USDA_DESCRIBED_PORTION = re.compile(
    r"^(?:(?P<numerator>\d+)/(?P<denominator>[1-9]\d*)|(?P<whole>\d+(?:\.\d+)?))\s+(?P<label>.+)$"
)


class CsvSourceConfig(TypedDict):
    id: tuple[str, ...]
    name: tuple[str, ...]
    nutrients: dict[str, tuple[str, ...]]


CSV_FIELDS: dict[str, CsvSourceConfig] = {
    "cofid": {
        "id": ("Food Code", "Food code"),
        "name": ("Food Name", "Food name"),
        "nutrients": {
            "kcal": ("Energy (kcal)",),
            "protein_g": ("Protein (g)",),
            "fibre_g": ("AOAC fibre (g)", "Fibre (g)"),
            "carbs_g": ("Carbohydrate (g)",),
            "fat_g": ("Fat (g)",),
            "water_ml": ("Water (g)",),
        },
    },
    "ciqual": {
        "id": ("alim_code",),
        "name": ("alim_nom_fr",),
        "nutrients": {
            "kcal": ("Energie, Règlement UE N° 1169 2011 (kcal",),
            "protein_g": ("Protéines, N x facteur de Jones",),
            "fibre_g": ("Fibres alimentaires",),
            "carbs_g": ("Glucides",),
            "fat_g": ("Lipides",),
            "water_ml": ("Eau",),
        },
    },
    "bls": {
        "id": ("BLS Code", "BLS_CODE", "SBLS"),
        "name": ("Lebensmittelbezeichnung", "NAME", "ST"),
        "nutrients": {
            "kcal": ("ENERCC Energie", "GCAL"),
            "protein_g": ("PROT625 Protein", "EWP"),
            "fibre_g": ("FIBT Ballaststoffe", "ZELL"),
            "carbs_g": ("CHO Kohlenhydrate", "KHC"),
            "fat_g": ("FAT Fett", "FC"),
            "water_ml": ("WATER Wasser", "WASS"),
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


def _usda_described_portion(description: str) -> tuple[float, str] | None:
    match = USDA_DESCRIBED_PORTION.match(description)
    if match is None:
        return None
    amount = (
        int(match["numerator"]) / int(match["denominator"]) if match["numerator"] is not None else float(match["whole"])
    )
    unit = re.split(r"\s*(?:,|\(|\bor\b)", match["label"], maxsplit=1, flags=re.IGNORECASE)[0].strip()
    return (amount, unit[:64].rstrip()) if amount > 0 and unit else None


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
        if not isinstance(source, dict):
            continue
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
            description = str(portion.get("portionDescription") or portion.get("modifier") or unit).strip()
            if (amount is None or unit.casefold() == "undetermined") and (
                described := _usda_described_portion(description)
            ):
                amount, unit = described
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


def _header(row: Mapping[str, str | None], prefixes: tuple[str, ...]) -> str | None:
    for header in row:
        normalized = re.sub(r"[/\s]+", " ", header).strip()
        if any(normalized.startswith(prefix) for prefix in prefixes):
            return header
    return None


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
        source_id_counts: dict[str, int] = {}
        for row in csv.DictReader(source, delimiter=dialect.delimiter, quotechar='"', doublequote=True):
            source_food_id = _first(row, config["id"])
            name = _first(row, config["name"])
            if not source_food_id or not name:
                continue
            occurrence = source_id_counts.get(source_food_id, 0) + 1
            source_id_counts[source_food_id] = occurrence
            if occurrence > 1:
                suffix = f"#{occurrence}"
                source_food_id = f"{source_food_id[: 255 - len(suffix)]}{suffix}"
            nutrients = _empty_core()
            for code, prefixes in config["nutrients"].items():
                if header := _header(row, prefixes):
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
            yield SourceFoodRow(
                source_food_id=source_food_id,
                name=name,
                preparation_state=_preparation_state(name),
                nutrients_per_100g=nutrients,
                raw_nutrients=raw,
            )
