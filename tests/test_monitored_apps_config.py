"""Safe loading for local monitored-app credentials."""

from __future__ import annotations

from core.monitored_apps import load_app_configs


def test_runtime_demo_token_uses_the_supplied_environment_value(tmp_path, monkeypatch) -> None:
    path = tmp_path / "monitored_apps.yaml"
    path.write_text(
        "apps:\n  - name: runtime_demo\n    ingest_token: ${SENTINEL_INGEST_TOKEN}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("SENTINEL_INGEST_TOKEN", "runtime-demo-test-token")

    apps = load_app_configs(path)

    assert apps[0]["ingest_token"] == "runtime-demo-test-token"


def test_runtime_demo_token_is_ephemeral_when_environment_value_is_missing(
    tmp_path, monkeypatch
) -> None:
    path = tmp_path / "monitored_apps.yaml"
    path.write_text(
        "apps:\n  - name: runtime_demo\n    ingest_token: ${SENTINEL_INGEST_TOKEN}\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("SENTINEL_INGEST_TOKEN", raising=False)

    apps = load_app_configs(path)

    assert apps[0]["ingest_token"] != "${SENTINEL_INGEST_TOKEN}"
    assert len(apps[0]["ingest_token"]) >= 32
