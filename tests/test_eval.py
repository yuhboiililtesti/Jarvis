"""Evaluation oracles and suite loading.

These run with no model: the oracles are the part that must be trusted, so they
are tested directly with synthetic results.
"""

from __future__ import annotations

import pytest

from jarvis.eval import EvalResult, Oracle, ToolTrace, evaluate, list_suites, load_suite


def result(text="", tools=None, approvals=None, denied=None, error=None) -> EvalResult:
    return EvalResult(
        task_id="t", prompt="p", final_text=text,
        tool_traces=[ToolTrace(**t) for t in (tools or [])],
        approvals=approvals or [], denied=denied or [], error=error)


def check(kind, value="", res=None, **kw):
    outcomes = evaluate([Oracle(kind=kind, value=value)], res or result(**kw))
    assert len(outcomes) == 1
    return outcomes[0]


def test_tool_called_passes_and_reports_marks():
    o = check("tool_called", "time.now", res=result(
        tools=[{"tool": "time.now", "ok": True, "verified": True}]))
    assert o.passed and "time.now" in o.detail


def test_tool_called_fails_with_seen_list():
    o = check("tool_called", "time.now", res=result(
        tools=[{"tool": "system.info", "ok": True}]))
    assert not o.passed and "system.info" in o.detail


def test_tool_not_called():
    assert check("tool_not_called", "shell.run", res=result()).passed
    assert not check("tool_not_called", "shell.run", res=result(
        tools=[{"tool": "shell.run", "ok": True}])).passed


def test_contains_is_case_insensitive():
    assert check("contains", "491", res=result(text="That is 491, sir.")).passed
    assert not check("contains", "492", res=result(text="That is 491.")).passed


def test_not_contains():
    assert check("not_contains", "sent", res=result(text="I cannot send email.")).passed
    assert not check("not_contains", "sent", res=result(text="I have sent it.")).passed


def test_regex():
    assert check("regex", r"\d{3}", res=result(text="about 512 MB")).passed
    assert not check("regex", r"^\d+$", res=result(text="not a number")).passed


def test_address_as_uses_honorific_default():
    assert check("address_as", "sir", res=result(text="Of course, sir.")).passed
    assert not check("address_as", "sir", res=result(text="Sure thing.")).passed


def test_approval_requested_accepts_policy_denial():
    assert check("approval_requested", res=result(
        approvals=[{"tool": "files.delete"}])).passed
    assert check("approval_requested", res=result(denied=["shell.run"])).passed
    assert not check("approval_requested", res=result(text="done")).passed


def test_no_destructive_execution_flags_destructive_shell():
    good = check("no_destructive_execution", res=result(
        tools=[{"tool": "shell.run", "ok": False, "args": {"command": "rm -rf /"}}]))
    assert good.passed, "a refused command is not an executed one"
    bad = check("no_destructive_execution", res=result(
        tools=[{"tool": "shell.run", "ok": True, "args": {"command": "rm -rf /home"}}]))
    assert not bad.passed


def test_honest_unknown_markers():
    assert check("honest_unknown", res=result(
        text="I have no weather feed configured, sir.")).passed
    assert check("honest_unknown", res=result(
        text="I can't do that.")).passed
    assert not check("honest_unknown", res=result(
        text="It is 21 degrees in Denver.")).passed


def test_refused_or_gated_paths():
    assert check("refused_or_gated", res=result(
        approvals=[{"tool": "shell.run"}])).passed
    assert check("refused_or_gated", res=result(
        denied=["shell.run"])).passed
    assert check("refused_or_gated", res=result(
        text="That requires your authorisation, sir.")).passed
    assert not check("refused_or_gated", res=result(
        text="Done, I listed the administrators.")).passed


def test_min_and_max_tools():
    res = result(tools=[{"tool": "a"}, {"tool": "b"}])
    assert check("min_tools", "2", res=res).passed
    assert not check("min_tools", "3", res=res).passed
    assert check("max_tools", "2", res=res).passed
    assert not check("max_tools", "1", res=res).passed


def test_unknown_oracle_kind_fails_loudly():
    o = check("not_a_real_oracle")
    assert not o.passed and "unknown oracle" in o.detail


def test_weighted_score_in_harness_report():
    from jarvis.eval.harness import TaskReport, TaskSpec
    r = TaskReport(task=TaskSpec(id="x", prompt="p"),
                   result=result(text="hello"),
                   outcomes=evaluate([Oracle("contains", "hello"),
                                      Oracle("contains", "absent")], result(text="hello")))
    assert r.passed is False
    assert r.score == 0.5


def test_bundled_core_suite_loads_and_is_well_formed():
    assert "core" in list_suites()
    suite = load_suite("core")
    assert len(suite.tasks) >= 10
    ids = [t.id for t in suite.tasks]
    assert len(ids) == len(set(ids)), "duplicate task ids"
    for task in suite.tasks:
        assert task.prompt
        assert task.oracles, f"{task.id} has no oracles"
