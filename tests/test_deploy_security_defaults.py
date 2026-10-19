from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
CHART = ROOT / "deploy" / "helm" / "selfheal"


def test_helm_chart_sets_restrictive_security_context_for_project_containers() -> None:
    values = yaml.safe_load((CHART / "values.yaml").read_text(encoding="utf-8"))
    context = values["containerSecurityContext"]

    assert context["allowPrivilegeEscalation"] is False
    assert context["capabilities"]["drop"] == ["ALL"]
    assert context["runAsNonRoot"] is True
    assert context["runAsUser"] == 10001
    assert context["seccompProfile"]["type"] == "RuntimeDefault"

    for name in ("app", "healer", "mcp", "sentinel", "migrate-job"):
        template = (CHART / "templates" / f"{name}.yaml").read_text(encoding="utf-8")
        assert "toYaml .Values.containerSecurityContext | nindent 12" in template


def test_project_docker_images_run_as_the_chart_security_context_user() -> None:
    for name in ("app", "healer", "mcp", "sentinel"):
        dockerfile = ROOT / "deploy" / "docker" / f"Dockerfile.{name}"
        assert "USER 10001:10001" in dockerfile.read_text(encoding="utf-8")
