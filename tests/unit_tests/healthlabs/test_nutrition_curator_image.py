import tomllib
from pathlib import Path

ROOT = Path(__file__).parents[3]


def test_production_image_can_start_curator_worker() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    entrypoint = (ROOT / "docker/entry.sh").read_text()

    assert project["project"]["scripts"]["mealie-nutrition-curator"] == (
        "mealie.services.healthlabs.nutrition_curator_runner:main"
    )
    assert 'change_user "$@"' in entrypoint
    assert 'exec "$@"' in entrypoint
