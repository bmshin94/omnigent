"""E2E regression: the background local server must run the installed runtime.

``omnigent server --background`` / ``omnigent run`` / ``omnigent host`` (no
``--server``) spawn a detached local server as ``python -m omnigent.cli server
...`` from ``omnigent/host/local_server.py``. That child inherits the
launcher's working directory, and without Python safe-path mode ``-m`` puts
that directory at ``sys.path[0]`` — so a workspace containing a conflicting
``omnigent`` package (e.g. a stale checkout) shadows the installed runtime
inside the server child. The reported journey:

1. ``cd`` into a workspace that contains a conflicting ``omnigent/`` package,
2. run ``omnigent server --background``,
3. the spawned server resolves ``omnigent`` from the workspace instead of the
   installed runtime — a broken checkout fails the boot ("Background local
   server failed to start" with the workspace traceback), an importable one
   silently serves the workspace's code.

The CLI under test is spawned with ``-P`` (mirroring the installed ``omnigent``
console script, whose ``sys.path`` never contains the user's cwd), so only the
server child's own spawn can pick the workspace checkout up. Each test isolates
``$HOME`` to a tmp dir so the pidfile / DB / logs land under
``<home>/.omnigent`` and never touch the developer's real ``~/.omnigent``. No
LLM is needed::

    pytest tests/e2e/test_local_server_installed_runtime_e2e.py -v
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import httpx
import psutil

from tests.e2e.helpers import POLL_INTERVAL_S

_REPO_ROOT = Path(__file__).resolve().parents[2]

# `server --background` blocks until the detached child is healthy or dead;
# a cold boot (imports + SQLite migrations) fits the server's own 120s boot
# ceiling, leaving slack for the CLI's own imports.
_CLI_TIMEOUT_S = 240.0

# Env vars that would leak the coding-agent harness's own creds / config
# into the server under test or break HOME isolation (an explicit
# OMNIGENT_DATABASE_URI or data dir would escape the isolated tmp home).
_ENV_TO_CLEAR = (
    "DATABRICKS_TOKEN",
    "DATABRICKS_CONFIG_PROFILE",
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "CLAUDE_CODE",
    "CODEX",
    "OMNIGENT_DATA_DIR",
    "OMNIGENT_CONFIG_HOME",
    "OMNIGENT_AUTH_ENABLED",
    "OMNIGENT_OIDC_ISSUER",
    "OMNIGENT_AUTH_PROVIDER",
    "OMNIGENT_DATABASE_URI",
    "OMNIGENT_RUNNER_TUNNEL_TOKEN",
)

# A conflicting-but-importable workspace checkout: records every process that
# resolves ``omnigent`` from the workspace, then delegates to the real package
# so the process keeps working — the stale-checkout case, where the bug is
# silent wrong-runtime selection rather than a crash.
_DELEGATING_CHECKOUT = """\
import json as _json
import os as _os
import sys as _sys
from pathlib import Path as _Path

with open({marker!r}, "a") as _fh:
    _fh.write(_json.dumps({{"pid": _os.getpid(), "argv": list(_sys.orig_argv)}}) + "\\n")

_real_init = _Path({real_pkg!r}) / "__init__.py"
__path__ = [{real_pkg!r}]
exec(compile(_real_init.read_text(), str(_real_init), "exec"))
__file__ = str(_real_init)
"""

_BROKEN_CHECKOUT = 'raise RuntimeError("conflicting workspace omnigent checkout was imported")\n'


def _isolated_env(home: Path) -> dict[str, str]:
    """Build a subprocess env with an isolated ``$HOME`` and no leaked creds.

    PYTHONPATH pins the worktree checkout so the subprocess imports the branch
    under test, not a stale installed wheel.

    :param home: The tmp home dir; ``<home>/.omnigent`` holds the pidfile,
        DB, logs, and artifacts for this test.
    :returns: The environment dict for ``subprocess.run``.
    """
    env = dict(os.environ)
    for key in _ENV_TO_CLEAR:
        env.pop(key, None)
    env["HOME"] = str(home)
    env["PYTHONPATH"] = f"{_REPO_ROOT}{os.pathsep}{env.get('PYTHONPATH', '')}"
    return env


def _write_conflicting_checkout(workspace: Path, init_body: str) -> None:
    """Create ``<workspace>/omnigent/__init__.py`` with the given body.

    :param workspace: The user's workspace directory (the CLI's cwd).
    :param init_body: Source for the conflicting package's ``__init__.py``.
    """
    pkg = workspace / "omnigent"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text(init_body)


def _run_background_server(
    workspace: Path, env: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    """Run ``omnigent server --background`` from inside the workspace.

    :param workspace: Directory to run the CLI from (contains the conflicting
        ``omnigent/`` package).
    :param env: Isolated subprocess environment.
    :returns: The completed CLI invocation with captured output.
    """
    return subprocess.run(
        [sys.executable, "-P", "-m", "omnigent.cli", "server", "--background"],
        cwd=str(workspace),
        env=env,
        capture_output=True,
        text=True,
        timeout=_CLI_TIMEOUT_S,
    )


def _pidfile_path(home: Path) -> Path:
    """Return the canonical local-server pidfile path under an isolated home.

    :param home: The isolated home dir.
    :returns: ``<home>/.omnigent/local_server.pid``.
    """
    return home / ".omnigent" / "local_server.pid"


def _read_pidfile(path: Path) -> tuple[int, int] | None:
    """Read ``<pid>\\n<port>\\n`` from the pidfile.

    :param path: Pidfile path.
    :returns: ``(pid, port)`` when well-formed, else ``None``.
    """
    try:
        lines = path.read_text().strip().splitlines()
    except OSError:
        return None
    if len(lines) < 2:
        return None
    try:
        return int(lines[0]), int(lines[1])
    except ValueError:
        return None


def _pid_alive(pid: int) -> bool:
    """Return whether a process id is currently alive.

    :param pid: Process id to probe.
    :returns: ``True`` if the process exists, ``False`` once it has exited.
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _health_ok(port: int) -> bool:
    """Return whether ``/health`` answers 200 on a loopback port.

    :param port: Loopback TCP port.
    :returns: ``True`` on HTTP 200, else ``False``.
    """
    try:
        resp = httpx.get(f"http://127.0.0.1:{port}/health", timeout=2.0, trust_env=False)
    except httpx.HTTPError:
        return False
    return resp.status_code == 200


def _read_marker(marker: Path) -> list[dict[str, object]]:
    """Read the workspace-import marker written by the delegating checkout.

    :param marker: Marker file path; absent when nothing imported the
        workspace package.
    :returns: One entry per process that resolved ``omnigent`` from the
        workspace, each with ``pid`` and interpreter ``argv``.
    """
    try:
        lines = marker.read_text().splitlines()
    except OSError:
        return []
    return [json.loads(line) for line in lines if line.strip()]


def _stop_pidfile_server(home: Path) -> None:
    """Stop the detached server recorded in the isolated home's pidfile.

    The server is spawned detached (``start_new_session``), so the CLI's exit
    never reaps it — without this it would leak past the test.

    :param home: The isolated home dir.
    """
    entry = _read_pidfile(_pidfile_path(home))
    if entry is None or not _pid_alive(entry[0]):
        return
    pid = entry[0]
    with contextlib.suppress(OSError):
        os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            return
        time.sleep(POLL_INTERVAL_S)
    with contextlib.suppress(OSError):
        os.kill(pid, signal.SIGKILL)


def test_local_server_child_ignores_conflicting_workspace_checkout(tmp_path: Path) -> None:
    """A healthy boot must not resolve ``omnigent`` from the workspace cwd.

    The workspace checkout here is importable (it delegates to the real
    package after recording the import), so the buggy child still boots
    healthy — running workspace-selected code. The regression is the marker:
    no process of the spawned server may resolve ``omnigent`` from the
    workspace. The child must also keep the workspace as its working
    directory (expected behavior preserves it; only the import path changes).
    """
    home = tmp_path / "home"
    home.mkdir()
    workspace = tmp_path / "workspace"
    marker = tmp_path / "workspace-imports.jsonl"
    _write_conflicting_checkout(
        workspace,
        _DELEGATING_CHECKOUT.format(marker=str(marker), real_pkg=str(_REPO_ROOT / "omnigent")),
    )

    try:
        result = _run_background_server(workspace, _isolated_env(home))
        assert result.returncode == 0, (
            f"`server --background` failed (rc={result.returncode}).\n"
            f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
        )
        entry = _read_pidfile(_pidfile_path(home))
        assert entry is not None and _pid_alive(entry[0]) and _health_ok(entry[1]), (
            f"background server not healthy after a successful spawn: {entry}"
        )

        workspace_imports = _read_marker(marker)
        assert not workspace_imports, (
            "the local-server child resolved `omnigent` from the workspace "
            f"checkout instead of the installed runtime:\n{workspace_imports}"
        )
        child_cwd = Path(psutil.Process(entry[0]).cwd()).resolve()
        assert child_cwd == workspace.resolve(), (
            f"local server child did not preserve the workspace working "
            f"directory: cwd={child_cwd}, workspace={workspace.resolve()}"
        )
    finally:
        _stop_pidfile_server(home)


def test_local_server_boots_despite_broken_workspace_checkout(tmp_path: Path) -> None:
    """A broken conflicting checkout must not take down local-server startup.

    With the workspace package raising at import, the buggy server child
    crashes before binding and the user sees "Background local server failed
    to start" with the workspace traceback in the log tail. The fixed child
    never imports the workspace package: the boot succeeds and the pidfile
    server answers ``/health``.
    """
    home = tmp_path / "home"
    home.mkdir()
    workspace = tmp_path / "workspace"
    _write_conflicting_checkout(workspace, _BROKEN_CHECKOUT)

    try:
        result = _run_background_server(workspace, _isolated_env(home))
        assert result.returncode == 0, (
            f"`server --background` failed from a workspace with a broken "
            f"conflicting checkout (rc={result.returncode}).\n"
            f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
        )
        entry = _read_pidfile(_pidfile_path(home))
        assert entry is not None and _pid_alive(entry[0]) and _health_ok(entry[1]), (
            f"background server not healthy after a successful spawn: {entry}"
        )
    finally:
        _stop_pidfile_server(home)
