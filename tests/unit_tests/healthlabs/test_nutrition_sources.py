import json
from pathlib import Path

import pytest

from mealie.services.healthlabs.nutrition_sources import parse_source_file, verify_source_checksum

CORE_NUTRIENTS = {"kcal", "protein_g", "fibre_g", "carbs_g", "fat_g", "water_ml"}


def _amounts(path: Path, provider: str) -> tuple[dict[str, float | None], dict[str, object]]:
    row = next(iter(parse_source_file(provider, path)))
    return (
        {key: value.amount for key, value in row.nutrients_per_100g.items()},
        row.raw_nutrients,
    )


def test_usda_fooddata_central_json_maps_core_and_preserves_other_nutrients(tmp_path: Path) -> None:
    source = tmp_path / "usda.json"
    source.write_text(
        json.dumps(
            {
                "FoundationFoods": [
                    {
                        "fdcId": 172420,
                        "description": "Lentils, mature seeds, cooked, boiled",
                        "foodNutrients": [
                            {"nutrient": {"id": 1008, "name": "Energy", "unitName": "kcal"}, "amount": 116},
                            {"nutrient": {"id": 1003, "name": "Protein", "unitName": "g"}, "amount": 9.02},
                            {"nutrient": {"id": 1079, "name": "Fiber, total dietary", "unitName": "g"}, "amount": 7.9},
                            {
                                "nutrient": {"id": 1005, "name": "Carbohydrate, by difference", "unitName": "g"},
                                "amount": 20.13,
                            },
                            {"nutrient": {"id": 1004, "name": "Total lipid (fat)", "unitName": "g"}, "amount": 0.38},
                            {"nutrient": {"id": 1051, "name": "Water", "unitName": "g"}, "amount": 69.64},
                            {"nutrient": {"id": 1087, "name": "Calcium, Ca", "unitName": "mg"}, "amount": 19},
                        ],
                        "foodPortions": [
                            {
                                "amount": 1,
                                "gramWeight": 198,
                                "modifier": "cup",
                                "measureUnit": {"name": "cup"},
                            }
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    row = next(iter(parse_source_file("usda", source)))

    assert set(row.nutrients_per_100g) == CORE_NUTRIENTS
    assert row.nutrients_per_100g["protein_g"].amount == 9.02
    assert row.raw_nutrients["Calcium, Ca"] == {"amount": 19.0, "unit": "mg"}
    assert row.preparation_state == "cooked"
    assert row.portions[0].grams == 198


@pytest.mark.parametrize(
    ("provider", "delimiter", "headers", "values", "extra_name"),
    [
        (
            "cofid",
            ",",
            [
                "Food Code",
                "Food Name",
                "Energy (kcal)",
                "Protein (g)",
                "AOAC fibre (g)",
                "Carbohydrate (g)",
                "Fat (g)",
                "Water (g)",
                "Total sugars (g)",
            ],
            ["11-001", "Lentils boiled", "116", "9.02", "7.9", "20.13", "0.38", "69.64", "1.8"],
            "Total sugars (g)",
        ),
        (
            "ciqual",
            ";",
            [
                "alim_code",
                "alim_nom_fr",
                "Energie, Règlement UE N° 1169/2011 (kcal/100 g)",
                "Protéines, N x facteur de Jones (g/100 g)",
                "Fibres alimentaires (g/100 g)",
                "Glucides (g/100 g)",
                "Lipides (g/100 g)",
                "Eau (g/100 g)",
                "Calcium (mg/100 g)",
            ],
            ["20591", "Lentille verte cuite", "116,0", "9,02", "7,9", "20,13", "0,38", "69,64", "19,0"],
            "Calcium (mg/100 g)",
        ),
        (
            "bls",
            ";",
            ["BLS_CODE", "NAME", "GCAL", "EWP", "ZELL", "KHC", "FC", "WASS", "CA"],
            ["H640100", "Linsen gegart", "116", "9.02", "7.9", "20.13", "0.38", "69.64", "19"],
            "CA",
        ),
    ],
)
def test_national_csv_maps_all_core_nutrients_and_preserves_extra_columns(
    tmp_path: Path,
    provider: str,
    delimiter: str,
    headers: list[str],
    values: list[str],
    extra_name: str,
) -> None:
    source = tmp_path / f"{provider}.csv"
    source.write_text(
        delimiter.join(headers) + "\n" + delimiter.join(values) + "\n",
        encoding="utf-8",
    )

    amounts, raw = _amounts(source, provider)

    assert set(amounts) == CORE_NUTRIENTS
    assert amounts == {
        "kcal": 116,
        "protein_g": 9.02,
        "fibre_g": 7.9,
        "carbs_g": 20.13,
        "fat_g": 0.38,
        "water_ml": 69.64,
    }
    assert extra_name in raw


def test_non_numeric_or_less_than_values_stay_unknown(tmp_path: Path) -> None:
    source = tmp_path / "ciqual.csv"
    source.write_text(
        "alim_code;alim_nom_fr;Energie, Règlement UE N° 1169/2011 (kcal/100 g);"
        "Protéines, N x facteur de Jones (g/100 g)\n"
        "1;Test food;<0,5;-\n",
        encoding="utf-8",
    )

    row = next(iter(parse_source_file("ciqual", source)))

    assert row.nutrients_per_100g["kcal"].amount is None
    assert row.nutrients_per_100g["protein_g"].amount is None


def test_checksum_is_verified_before_parsing(tmp_path: Path) -> None:
    source = tmp_path / "source.csv"
    source.write_bytes(b"authoritative bytes")

    with pytest.raises(ValueError, match="checksum"):
        verify_source_checksum(source, "0" * 64)
