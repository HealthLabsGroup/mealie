import json
from uuid import uuid4


def test_cli_dry_run_never_reads_stdin_or_writes_checkpoint(tmp_path, monkeypatch, capsys):
    from mealie.services.healthlabs import recipe_metadata_cli as module

    group = str(uuid4())
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps([{"recipe_id": str(uuid4()), "group_id": group}]))
    identities = tmp_path / "identities.json"
    identities.write_text(
        json.dumps(
            {
                "identity_ids": {group: str(uuid4())},
                "name": "Session Azure",
                "base_url": "https://azure.example/v1",
                "model": "deployment",
            }
        )
    )
    checkpoint = tmp_path / "checkpoint.json"

    class NoRead:
        def read(self, *args):
            raise AssertionError("dry-run read stdin")

    async def run(manifest, **kwargs):
        assert kwargs["apply"] is False and kwargs["provider_resolver"] is None
        return {"denominator": 1, "complete": 0}

    monkeypatch.setattr(module, "run_manifest", run)
    monkeypatch.setattr(module.sys, "stdin", NoRead())
    monkeypatch.setattr(
        module.sys,
        "argv",
        ["run", str(manifest), "--provider-identities", str(identities), "--checkpoint", str(checkpoint)],
    )

    def no_git(*args, **kwargs):
        raise FileNotFoundError("git is not installed")

    monkeypatch.setattr(module.subprocess, "run", no_git)
    assert module.main() == 0
    result = json.loads(capsys.readouterr().out)
    assert result["complete"] == 0 and result["code_version"] == "installed-package"
    assert not checkpoint.exists()
