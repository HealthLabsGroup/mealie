"""Development wrapper; installed images use python -m mealie.services.healthlabs.recipe_metadata_cli."""

from mealie.services.healthlabs.recipe_metadata_cli import main

if __name__ == "__main__":
    raise SystemExit(main())
