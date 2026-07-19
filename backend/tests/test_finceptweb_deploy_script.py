from __future__ import annotations

import fcntl
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from pydantic import ValidationError

from api.admin.schemas import DeployStatusOut


SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "finceptweb-deploy.sh"


def _executable(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(0o755)


def _sandbox(tmp_path: Path) -> tuple[dict[str, str], Path, Path]:
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / "var").mkdir()
    (repo / "scripts").mkdir()
    shutil.copy2(SCRIPT, repo / "scripts" / SCRIPT.name)

    calls = tmp_path / "calls.log"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()

    _executable(
        fake_bin / "git",
        """#!/bin/sh
printf 'git %s\\n' "$*" >> "$FAKE_CALLS"
if [ "$1" = "-C" ]; then shift 2; fi
case "$1:$2:$3" in
  rev-parse:--short=12:HEAD)
    if [ -f "$FAKE_STATE/pulled" ]; then echo fedcba987654; else echo 0123456789ab; fi ;;
  rev-parse:--show-toplevel:*) echo "${FAKE_TOP_LEVEL:-$REPO}" ;;
  remote:get-url:origin) echo "${FAKE_REMOTE:-$EXPECTED_REMOTE}" ;;
  symbolic-ref:--quiet:--short) echo "${FAKE_BRANCH:-$BRANCH}" ;;
  diff:--quiet:*) [ "${FAKE_TRACKED_DIRTY:-0}" != 1 ] ;;
  diff:--cached:--quiet) [ "${FAKE_STAGED_DIRTY:-0}" != 1 ] ;;
  fetch:*) exit 0 ;;
  merge:--ff-only:*) touch "$FAKE_STATE/pulled" ;;
esac
""",
    )
    _executable(
        fake_bin / "docker-compose",
        """#!/bin/sh
printf 'docker-compose %s\\n' "$*" >> "$FAKE_CALLS"
case " $* " in
  *" config --format json "*)
    printf '%s\\n' '{"services":{"backend":{"environment":{"JWT_SECRET_KEY":"test-secret-key-32-characters-long"}}}}'
    exit 0 ;;
esac
case " $* " in
  *" run "*" migrate "*)
    [ "${FAKE_MIGRATE_FAIL:-0}" != 1 ] || exit 42 ;;
esac
case " $* " in
  *" run "*" db-backup "*)
    [ "${FAKE_BACKUP_FAIL:-0}" != 1 ] || exit 43 ;;
esac
if [ "${3:-}" = "ps" ] && [ "${4:-}" = "-q" ]; then
  printf '%s-cid\\n' "$5"
fi
exit 0
""",
    )
    _executable(
        fake_bin / "docker",
        """#!/bin/sh
printf 'docker %s\\n' "$*" >> "$FAKE_CALLS"
case "$3" in
  *State.Status*) echo "${FAKE_CONTAINER_STATE:-running}" ;;
  *State.Health*) echo "${FAKE_CONTAINER_HEALTH:-healthy}" ;;
  *) exit 2 ;;
esac
""",
    )
    _executable(
        fake_bin / "curl",
        """#!/bin/sh
printf 'curl %s\\n' "$*" >> "$FAKE_CALLS"
[ "${FAKE_HEALTH_FAIL:-0}" != 1 ]
""",
    )
    _executable(fake_bin / "sleep", "#!/bin/sh\nexit 0\n")

    env = {
        **os.environ,
        "PATH": f"{fake_bin}:/usr/bin:/bin",
        "REPO": str(repo),
        "BRANCH": "main",
        "COMPOSE_PROJECT_NAME": "finceptweb99-test",
        "HEALTH_URL": "http://127.0.0.1:8081/api/health",
        "INSTALL_PATH": str(tmp_path / "installed" / "finceptweb99-deploy.sh"),
        "EXPECTED_REMOTE": "https://github.com/example/FinceptWeb99.git",
        "FAKE_CALLS": str(calls),
        "FAKE_STATE": str(tmp_path),
    }
    Path(env["INSTALL_PATH"]).parent.mkdir()
    (repo / "var" / "deploy-meta.json").write_text(
        json.dumps({"actor": "admin-123", "trigger_id": "trigger-456"})
    )
    return env, repo, calls


def _run(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(SCRIPT)],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


@pytest.mark.parametrize(
    "missing",
    [
        "REPO",
        "BRANCH",
        "COMPOSE_PROJECT_NAME",
        "HEALTH_URL",
        "INSTALL_PATH",
        "EXPECTED_REMOTE",
    ],
)
def test_required_target_configuration_is_fail_closed(tmp_path: Path, missing: str) -> None:
    env, repo, calls = _sandbox(tmp_path)
    env.pop(missing)

    result = _run(env)

    assert result.returncode == 64
    assert not (repo / "var" / "deploy-status.json").exists()
    assert not calls.exists()


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("FAKE_TOP_LEVEL", "/wrong/repository"),
        ("FAKE_REMOTE", "https://github.com/example/wrong.git"),
        ("FAKE_BRANCH", "develop"),
        ("FAKE_TRACKED_DIRTY", "1"),
        ("FAKE_STAGED_DIRTY", "1"),
    ],
)
def test_preflight_identity_and_dirty_checks_fail_before_deploy_actions(
    tmp_path: Path, key: str, value: str
) -> None:
    env, repo, calls = _sandbox(tmp_path)
    env[key] = value

    result = _run(env)

    assert result.returncode != 0
    status = json.loads((repo / "var" / "deploy-status.json").read_text())
    assert status["phase"] == "failed"
    assert "starting failed" in status["error"]
    call_text = calls.read_text()
    assert " docker-compose " not in f" {call_text} "
    assert " fetch " not in call_text
    assert " merge " not in call_text


def test_backup_precedes_pull_and_migration_and_all_images_are_restarted(tmp_path: Path) -> None:
    env, repo, calls = _sandbox(tmp_path)

    result = _run(env)

    assert result.returncode == 0, result.stderr
    status = json.loads((repo / "var" / "deploy-status.json").read_text())
    assert status["phase"] == "completed"
    assert status["before_sha"] == "0123456789ab"
    assert status["after_sha"] == "fedcba987654"
    assert status["branch"] == "main"
    assert status["actor"] == "admin-123"
    assert status["trigger_id"] == "trigger-456"
    assert status["error"] is None

    lines = calls.read_text().splitlines()
    backup_index = next(i for i, line in enumerate(lines) if "run --rm --no-deps" in line and "db-backup python" in line)
    pull_index = next(i for i, line in enumerate(lines) if " fetch --prune origin main" in line)
    migration_index = next(i for i, line in enumerate(lines) if "run --rm --no-deps migrate" in line)
    assert backup_index < pull_index < migration_index
    assert any("build backend frontend migrate scheduler db-backup" in line for line in lines)
    assert any("stop backend scheduler" in line for line in lines)
    assert any("up -d --no-deps backend scheduler frontend db-backup" in line for line in lines)
    assert any("up -d --no-deps nginx" in line for line in lines)
    assert Path(env["INSTALL_PATH"]).read_bytes() == SCRIPT.read_bytes()


@pytest.mark.parametrize(
    ("failure", "expected_error", "forbidden_call"),
    [
        ("FAKE_MIGRATE_FAIL", "migrating failed", " up -d "),
        ("FAKE_HEALTH_FAIL", "verifying failed", " deploy completed "),
    ],
)
def test_migration_and_health_failures_never_report_completed(
    tmp_path: Path, failure: str, expected_error: str, forbidden_call: str
) -> None:
    env, repo, calls = _sandbox(tmp_path)
    env[failure] = "1"

    result = _run(env)

    assert result.returncode != 0
    status = json.loads((repo / "var" / "deploy-status.json").read_text())
    assert status["phase"] == "failed"
    assert expected_error in status["error"]
    assert status["finished_at"] is not None
    assert forbidden_call not in calls.read_text()


def test_backup_failure_stops_before_fetch(tmp_path: Path) -> None:
    env, repo, calls = _sandbox(tmp_path)
    env["FAKE_BACKUP_FAIL"] = "1"

    result = _run(env)

    assert result.returncode != 0
    status = json.loads((repo / "var" / "deploy-status.json").read_text())
    assert status["phase"] == "failed"
    assert "backing_up failed" in status["error"]
    assert " fetch " not in calls.read_text()


def test_parallel_trigger_is_rejected_without_clobbering_active_status(tmp_path: Path) -> None:
    env, repo, _ = _sandbox(tmp_path)
    status_path = repo / "var" / "deploy-status.json"
    status_path.write_text('{"phase":"migrating","trigger_id":"active"}\n')
    lock_path = repo / "var" / "deploy.lock"

    with lock_path.open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = _run(env)

    assert result.returncode == 75
    assert json.loads(status_path.read_text()) == {
        "phase": "migrating",
        "trigger_id": "active",
    }


@pytest.mark.parametrize("phase", ["backing_up", "migrating"])
def test_deploy_status_accepts_hardened_phases(phase: str) -> None:
    assert DeployStatusOut(phase=phase).phase == phase


def test_deploy_status_rejects_unrecognized_phase() -> None:
    with pytest.raises(ValidationError):
        DeployStatusOut(phase="silently_skipped")
