"""P1-5/P1-6: the pinned runtime is consistent, and the GitHub automation keeps its
security properties (read-only token, SHA-pinned actions, no pull_request_target, no
expression interpolation inside shell scripts). Static text checks; offline."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = sorted((ROOT / ".github" / "workflows").glob("*.yml"))


def test_python_runtime_is_one_pinned_312_patch_release():
    pinned = (ROOT / ".python-version").read_text().strip()
    assert re.fullmatch(r"3\.12\.\d+", pinned)
    installer = (ROOT / "scripts" / "install-macos.sh").read_text()
    assert f'PYTHON_VERSION="{pinned}"' in installer
    assert 'uv tool install --force --python "$PYTHON_VERSION"' in installer


def test_workflows_exist():
    assert {p.name for p in WORKFLOWS} == {"ci.yml", "security.yml"}
    assert (ROOT / ".github" / "dependabot.yml").exists()


def test_workflow_permissions_are_read_only():
    for path in WORKFLOWS:
        text = path.read_text()
        assert re.search(r"^permissions:\n  contents: read\n", text, re.M), path.name
        assert "write" not in re.sub(r"#.*", "", text), path.name
        assert "pull_request_target" not in text and "workflow_run" not in text, path.name
        assert "secrets." not in text, path.name


def test_every_action_is_pinned_to_a_full_commit_sha():
    uses = [
        line.strip()
        for path in WORKFLOWS
        for line in path.read_text().splitlines()
        if line.strip().startswith(("uses:", "- uses:"))
    ]
    assert uses
    for line in uses:
        assert re.fullmatch(r"(- )?uses: [\w.-]+/[\w.-]+@[0-9a-f]{40} # v\d+\.\d+\.\d+", line), line


def test_expressions_never_reach_shell_scripts_directly():
    for path in WORKFLOWS:
        for line in path.read_text().splitlines():
            if "${{" in line:
                # Only env assignments and the concurrency group may use expressions;
                # scripts read them as quoted environment variables.
                assert re.match(r"^\s+([A-Z_]+|group): .*\$\{\{ github\.[\w.]+ \}\}", line), line


def test_downloaded_tools_are_verified():
    security = (ROOT / ".github" / "workflows" / "security.yml").read_text()
    assert re.search(r"GITLEAKS_SHA256: [0-9a-f]{64}\n", security)
    assert "sha256sum --check --strict" in security
    assert "--redact" in security
    assert "--only-group audit pip-audit" in security and "--require-hashes" in security
    pyproject = (ROOT / "pyproject.toml").read_text()
    assert re.search(r'audit = \[\n    "pip-audit==[\d.]+",\n\]', pyproject)


def test_dependency_audit_ignores_no_advisory():
    """P2-8: the PyJWT upgrade closed every accepted advisory; any advisory now fails."""
    security = (ROOT / ".github" / "workflows" / "security.yml").read_text()
    assert "--ignore-vuln" not in security and "--skip" not in security
    assert "ACCEPTED_ADVISORIES" not in security and "continue-on-error" not in security
    assert "--only-group audit pip-audit" in security and "--require-hashes" in security


def test_locked_pyjwt_is_the_patched_release():
    lock = (ROOT / "uv.lock").read_text()
    version = re.search(r'name = "pyjwt"\nversion = "([\d.]+)"', lock)
    assert version and tuple(map(int, version.group(1).split("."))) >= (2, 15, 1)
