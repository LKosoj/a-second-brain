"""Tests for scripts/install-launchd-user.sh.

The script is plain bash, so the tests run it under ``subprocess.run`` with
``uv`` and ``launchctl`` patched in the PATH and assert on rendered
plist files. We never call ``launchctl load`` -- the script's ``--enable``
mode is exercised only by hand on a real macOS host.

PATH isolation: tests replace the inherited PATH entirely rather than
prepending stubs to it. ``install-launchd-user.sh`` decides whether to
render the optional ``plaud-sync`` / ``qmd-maintenance`` agents with
``command -v qmd`` / a grep over ``.env``; if the host has ``qmd`` (or a
PLAUD_BEARER_TOKEN) installed outside the stub dir, a prepend-only PATH
would still surface it and the test for "optional plists are skipped"
would fail. The fake PATH here is exactly ``<stub_dir>:/usr/bin:/bin``,
which is enough for bash, sed, grep, mkdir, cp, chmod, and the stubs
themselves, but cannot accidentally pick up a host install of ``qmd``.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "install-launchd-user.sh"

# Minimal PATH used by the install-script tests. Only the directories that
# ship the standard utilities the script and the bash interpreter need.
# ``/usr/local/bin``, ``/opt/homebrew/bin``, and user-local bins are
# deliberately excluded so that a host install of ``qmd`` (or anything
# else) cannot satisfy ``command -v qmd`` inside the test.
_ISOLATED_SYSTEM_PATH = "/usr/bin:/bin"


def _make_stub_dir(tmp_path: Path) -> Path:
    """Create a directory of fake uv and launchctl binaries on a fake PATH."""
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    (bin_dir / "uv").write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    (bin_dir / "launchctl").write_text(
        "#!/usr/bin/env bash\nexit 0\n",
        encoding="utf-8",
    )
    for stub in ("uv", "launchctl"):
        (bin_dir / stub).chmod(stat.S_IRWXU)
    return bin_dir


def _prepare_project(project_dir: Path) -> None:
    """Lay down the minimum files install-launchd-user.sh insists on.

    The script resolves ``PROJECT_DIR`` from its own location
    (``BASH_SOURCE`` -> ``..``), so we copy the install script + the
    plist templates + the env wrapper into the temp project. The real
    repo's .env is left alone.
    """
    project_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / ".env").write_text(
        "TELEGRAM_BOT_TOKEN=t\nDEEPGRAM_API_KEY=d\nOWNER_TELEGRAM_ID=1\n",
        encoding="utf-8",
    )
    (project_dir / "vault").mkdir()

    scripts_dst = project_dir / "scripts"
    scripts_dst.mkdir(exist_ok=True)
    wrapper_src = REPO_ROOT / "scripts" / "lib" / "run_with_env.sh"
    wrapper_dst = scripts_dst / "install-launchd-user.sh"
    shutil.copy(REPO_ROOT / "scripts" / "install-launchd-user.sh", wrapper_dst)
    wrapper_dst.chmod(stat.S_IRWXU)

    lib_dst = scripts_dst / "lib"
    lib_dst.mkdir(exist_ok=True)
    shutil.copy(wrapper_src, lib_dst / "run_with_env.sh")
    (lib_dst / "run_with_env.sh").chmod(stat.S_IRWXU)

    deploy_dst = project_dir / "deploy"
    deploy_dst.mkdir(exist_ok=True)
    for name in (
        "com.second-brain.bot.plist.in",
        "com.second-brain.process.plist.in",
        "com.second-brain.plaud-sync.plist.in",
        "com.second-brain.qmd-maintenance.plist.in",
    ):
        shutil.copy(
            REPO_ROOT / "deploy" / name,
            deploy_dst / name,
        )


@pytest.fixture
def fake_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Provide a stub-only PATH: stubs + system utilities, nothing else.

    Replaces the inherited PATH entirely so that ``command -v qmd`` and
    other host-installed binaries cannot accidentally satisfy optional
    prerequisites inside the script.
    """
    bin_dir = _make_stub_dir(tmp_path)
    monkeypatch.setenv("PATH", f"{bin_dir}:{_ISOLATED_SYSTEM_PATH}")
    return bin_dir


def test_install_renders_bot_plist(tmp_path: Path, fake_path: Path) -> None:
    project_dir = tmp_path / "project"
    _prepare_project(project_dir)

    home = tmp_path / "home"
    home.mkdir()
    agent_dir = home / "Library" / "LaunchAgents"
    log_dir = home / "Library" / "Logs" / "com.second-brain"

    env = os.environ.copy()
    env["HOME"] = str(home)

    subprocess.run(
        ["bash", str(project_dir / "scripts" / "install-launchd-user.sh")],
        cwd=tmp_path,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )

    bot_plist = agent_dir / "com.second-brain.bot.plist"
    process_plist = agent_dir / "com.second-brain.process.plist"
    assert bot_plist.exists()
    assert process_plist.exists()

    bot_body = bot_plist.read_text(encoding="utf-8")
    assert "@PROJECT_DIR@" not in bot_body
    assert "@UV_BIN@" not in bot_body
    assert "@WRAPPER@" not in bot_body
    assert "a-second-brain" in bot_body
    assert str(project_dir) in bot_body
    assert "com.second-brain.bot" in bot_body
    assert str(log_dir) in bot_body

    process_body = process_plist.read_text(encoding="utf-8")
    assert "d_brain.run_daily_process" in process_body
    assert "<key>Hour</key>" in process_body
    assert "<integer>21</integer>" in process_body


def test_install_skips_optional_plists(tmp_path: Path, fake_path: Path) -> None:
    project_dir = tmp_path / "project"
    _prepare_project(project_dir)

    home = tmp_path / "home"
    home.mkdir()
    env = os.environ.copy()
    env["HOME"] = str(home)

    # No PLAUD_BEARER_TOKEN and no qmd stub -- only bot + process should render.
    subprocess.run(
        ["bash", str(project_dir / "scripts" / "install-launchd-user.sh")],
        cwd=tmp_path,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )

    agent_dir = home / "Library" / "LaunchAgents"
    assert (agent_dir / "com.second-brain.bot.plist").exists()
    assert (agent_dir / "com.second-brain.process.plist").exists()
    assert not (agent_dir / "com.second-brain.plaud-sync.plist").exists()
    assert not (
        agent_dir / "com.second-brain.qmd-maintenance.plist"
    ).exists()


def test_install_includes_plaud_when_token_present(
    tmp_path: Path, fake_path: Path
) -> None:
    project_dir = tmp_path / "project"
    _prepare_project(project_dir)
    (project_dir / ".env").write_text(
        "TELEGRAM_BOT_TOKEN=t\n"
        "DEEPGRAM_API_KEY=d\n"
        "OWNER_TELEGRAM_ID=1\n"
        "PLAUD_BEARER_TOKEN=token\n",
        encoding="utf-8",
    )

    home = tmp_path / "home"
    home.mkdir()
    env = os.environ.copy()
    env["HOME"] = str(home)

    subprocess.run(
        ["bash", str(project_dir / "scripts" / "install-launchd-user.sh")],
        cwd=tmp_path,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )

    plaud_plist = (
        home
        / "Library"
        / "LaunchAgents"
        / "com.second-brain.plaud-sync.plist"
    )
    assert plaud_plist.exists()
    body = plaud_plist.read_text(encoding="utf-8")
    assert "d_brain.run_plaud_sync" in body
    assert "<key>StartInterval</key>" in body


def test_install_uninstall_removes_plists(tmp_path: Path, fake_path: Path) -> None:
    project_dir = tmp_path / "project"
    _prepare_project(project_dir)

    home = tmp_path / "home"
    home.mkdir()
    agent_dir = home / "Library" / "LaunchAgents"
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "com.second-brain.bot.plist").write_text(
        "<plist/>", encoding="utf-8"
    )

    env = os.environ.copy()
    env["HOME"] = str(home)

    subprocess.run(
        [
            "bash",
            str(project_dir / "scripts" / "install-launchd-user.sh"),
            "--uninstall",
        ],
        cwd=tmp_path,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )

    assert not (agent_dir / "com.second-brain.bot.plist").exists()


def test_install_rejects_unknown_arg(tmp_path: Path, fake_path: Path) -> None:
    project_dir = tmp_path / "project"
    _prepare_project(project_dir)

    home = tmp_path / "home"
    home.mkdir()
    env = os.environ.copy()
    env["HOME"] = str(home)

    result = subprocess.run(
        ["bash", str(project_dir / "scripts" / "install-launchd-user.sh"), "--bogus"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "Usage" in result.stderr


def test_install_fails_without_env(tmp_path: Path, fake_path: Path) -> None:
    project_dir = tmp_path / "project"
    project_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / "vault").mkdir()
    scripts_dst = project_dir / "scripts"
    scripts_dst.mkdir()
    shutil.copy(
        REPO_ROOT / "scripts" / "install-launchd-user.sh",
        scripts_dst / "install-launchd-user.sh",
    )
    (scripts_dst / "install-launchd-user.sh").chmod(stat.S_IRWXU)
    lib_dst = scripts_dst / "lib"
    lib_dst.mkdir()
    shutil.copy(
        REPO_ROOT / "scripts" / "lib" / "run_with_env.sh",
        lib_dst / "run_with_env.sh",
    )
    (lib_dst / "run_with_env.sh").chmod(stat.S_IRWXU)

    home = tmp_path / "home"
    home.mkdir()
    env = os.environ.copy()
    env["HOME"] = str(home)

    result = subprocess.run(
        ["bash", str(scripts_dst / "install-launchd-user.sh")],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "install.sh" in result.stderr


def test_wrapper_loads_env(tmp_path: Path) -> None:
    """The wrapper is the bridge between launchd and the project's .env."""
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    (project_dir / ".env").write_text(
        "FOO=bar\n# comment line\nQUOTED='hello world'\n",
        encoding="utf-8",
    )
    wrapper = REPO_ROOT / "scripts" / "lib" / "run_with_env.sh"

    result = subprocess.run(
        [str(wrapper), str(project_dir), "env"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "FOO=bar" in result.stdout
    assert "QUOTED=hello world" in result.stdout
    assert "comment line" not in result.stdout