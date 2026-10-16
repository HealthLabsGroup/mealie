#!/usr/bin/env python3

import argparse
import sys
from pathlib import Path

from mealie.schema.healthlabs.nutrition_source import SourceManifest
from mealie.services.healthlabs.nutrition_sources import parse_source_file, verify_source_checksum


def main() -> None:
    parser = argparse.ArgumentParser(description="Import a pinned HealthLabs nutrition source release")
    parser.add_argument("manifest", type=Path, help="JSON manifest with source, licence, version, and SHA-256")
    parser.add_argument("source", type=Path, help="Local USDA JSON or provider CSV file")
    args = parser.parse_args()

    manifest = SourceManifest.model_validate_json(args.manifest.read_text(encoding="utf-8"))
    verify_source_checksum(args.source, manifest.sha256)
    rows = parse_source_file(manifest.provider, args.source)

    from mealie.db.db_setup import session_context
    from mealie.services.healthlabs.nutrition_import_service import NutritionImportService

    with session_context() as session:
        result = NutritionImportService(session).import_release(manifest, rows)
    sys.stdout.write(f"{result.model_dump_json()}\n")


if __name__ == "__main__":
    main()
