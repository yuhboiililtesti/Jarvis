"""Remote code sandbox: script construction, limits, and (when the runner is
reachable) real execution with kernel-enforced caps.

The integration tests skip when the SSH runner is unavailable; the unit tests
always run.
"""

from __future__ import annotations

import pytest

from jarvis.sandbox.remote import (ALLOWED_INTERPRETERS, RemoteLimits,
                                   RemoteResult, RemoteSandbox, build_script)


# ---------------------------------------------------------------- units ----

def test_build_script_contains_limits_and_base64_program():
    script = build_script("print('hi')", limits=RemoteLimits(
        wall_seconds=3, memory_mb=128, cpu_seconds=2, file_mb=4))
    assert "ulimit -v 131072" in script          # 128 MiB in KiB
    assert "ulimit -t 2" in script
    assert "ulimit -f 4096" in script            # 4 MiB in KiB
    assert "timeout -s KILL 3" in script
    assert "python3 program.bin" in script
    assert "mktemp -d" in script and "trap 'rm -rf" in script
    assert "cHJpbnQoJ2hpJyk=" in script          # base64 of print('hi')


def test_build_script_args_are_quoted_not_injected():
    script = build_script("import sys; print(sys.argv[1:])", args=("a b", "x;rm -rf /"))
    assert "'a b'" in script
    assert "'x;rm -rf /'" in script
    assert ";rm -rf /" not in script.replace("'x;rm -rf /'", "")


def test_build_script_rejects_unknown_interpreter():
    with pytest.raises(ValueError):
        build_script("x", interpreter="rm")


def test_limits_validation():
    with pytest.raises(ValueError):
        RemoteLimits(wall_seconds=0).validate()
    with pytest.raises(ValueError):
        RemoteLimits(memory_mb=4).validate()
    RemoteLimits().validate()


def test_result_shape():
    result = RemoteResult(ok=True, exit_code=0, stdout="42\n", stderr="",
                          elapsed_s=0.2, host="laptop")
    data = result.to_dict()
    assert data["ok"] is True and data["tier"] == "remote-rlimit"
    assert data["elapsed_s"] == 0.2


# ------------------------------------------------------------ integration --

@pytest.fixture(scope="module")
def sandbox():
    box = RemoteSandbox()
    available, reason = box.available()
    if not available:
        pytest.skip(f"remote runner unavailable: {reason}")
    return box


def test_remote_runs_python(sandbox):
    result = sandbox.run("print(6 * 7)", limits=RemoteLimits(wall_seconds=10))
    assert result.ok, result.to_dict()
    assert "42" in result.stdout


def test_remote_wall_timeout_kills_runaway(sandbox):
    result = sandbox.run("import time; time.sleep(30)",
                         limits=RemoteLimits(wall_seconds=2, cpu_seconds=5))
    assert result.ok is False
    assert result.timed_out is True


def test_remote_memory_cap_is_enforced(sandbox):
    result = sandbox.run("x = bytearray(400 * 1024 * 1024); print(len(x))",
                         limits=RemoteLimits(wall_seconds=15, memory_mb=128))
    assert result.ok is False
    assert "MemoryError" in result.stderr


def test_remote_stdin_and_args(sandbox):
    result = sandbox.run(
        "import sys; data = sys.stdin.read().strip(); print(data.upper())",
        stdin_data="hello", limits=RemoteLimits(wall_seconds=10))
    assert result.ok, result.to_dict()
    assert "HELLO" in result.stdout


def test_remote_status_reports_truthfully(sandbox):
    status = sandbox.status()
    assert status["available"] is True
    assert status["network_isolated"] is False      # stated, not implied
    assert status["tier"] == "remote-rlimit"
