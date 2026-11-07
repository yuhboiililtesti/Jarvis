"""Evaluation harness.

Runs a held-out task suite through the real agent and scores it with
deterministic oracles. The point is to be able to say "this got worse" or "this
got better" with evidence, rather than vibes.

Isolation: each task runs in its own session against the live kernel, with
approval timeouts shortened so a suite cannot hang, and approvals recorded rather
than silently allowed. The harness never changes the configured autonomy mode.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from ..config import PROJECT_ROOT
from ..logging_setup import get_logger
from .oracles import EvalResult, Oracle, OracleOutcome, ToolTrace, evaluate

log = get_logger("jarvis.eval")

__all__ = ["EvalHarness", "Suite", "TaskSpec", "load_suite", "list_suites"]

SUITE_DIR = Path(__file__).parent / "suites"


@dataclass
class TaskSpec:
    id: str
    prompt: str
    oracles: list[Oracle] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    expect_tool: str = ""
    note: str = ""


@dataclass
class Suite:
    name: str
    description: str = ""
    tasks: list[TaskSpec] = field(default_factory=list)


@dataclass
class TaskReport:
    task: TaskSpec
    result: EvalResult
    outcomes: list[OracleOutcome] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(o.passed for o in self.outcomes)

    @property
    def score(self) -> float:
        total = sum(o.weight for o in self.outcomes) or 1.0
        got = sum(o.weight for o in self.outcomes if o.passed)
        return round(got / total, 3)


@dataclass
class SuiteReport:
    suite: str
    started: str
    finished: str = ""
    tasks: list[TaskReport] = field(default_factory=list)
    model: str = ""

    @property
    def passed(self) -> int:
        return sum(1 for t in self.tasks if t.passed)

    @property
    def total(self) -> int:
        return len(self.tasks)

    @property
    def score(self) -> float:
        return round(self.passed / self.total, 3) if self.total else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "suite": self.suite,
            "model": self.model,
            "started": self.started,
            "finished": self.finished,
            "passed": self.passed,
            "total": self.total,
            "score": self.score,
            "tasks": [
                {
                    "id": t.task.id,
                    "prompt": t.task.prompt,
                    "passed": t.passed,
                    "score": t.score,
                    "duration_s": round(t.result.duration_s, 2),
                    "tools": [tr.tool for tr in t.result.tool_traces],
                    "final_text": (t.result.final_text or "")[:600],
                    "error": t.result.error,
                    "oracles": [
                        {"kind": o.kind, "passed": o.passed, "detail": o.detail}
                        for o in t.outcomes
                    ],
                }
                for t in self.tasks
            ],
        }

    def to_markdown(self) -> str:
        lines = [
            f"# Evaluation — {self.suite}",
            "",
            f"**Model:** `{self.model}`  ",
            f"**Result:** {self.passed}/{self.total} passed ({self.score:.0%})  ",
            f"**When:** {self.started} → {self.finished}",
            "",
            "| Task | Result | Tools | Oracles failed |",
            "|---|---|---|---|",
        ]
        for t in self.tasks:
            failed = [o.kind for o in t.outcomes if not o.passed]
            mark = "PASS" if t.passed else "**FAIL**"
            tools = ", ".join(tr.tool for tr in t.result.tool_traces) or "—"
            lines.append(f"| `{t.task.id}` | {mark} | {tools} | {', '.join(failed) or '—'} |")
        lines += ["", "## Details", ""]
        for t in self.tasks:
            lines.append(f"### {t.task.id} — {'PASS' if t.passed else 'FAIL'}")
            lines.append(f"- prompt: {t.task.prompt}")
            for o in t.outcomes:
                lines.append(f"  - {'OK ' if o.passed else 'XX '} `{o.kind}` — {o.detail}")
            lines.append(f"- answer: {(t.result.final_text or '')[:400]}")
            lines.append("")
        return "\n".join(lines)


def load_suite(name_or_path: str | Path) -> Suite:
    path = Path(name_or_path)
    if not path.is_file():
        path = SUITE_DIR / f"{name_or_path}.json"
    if not path.is_file():
        raise FileNotFoundError(
            f"suite '{name_or_path}' not found; available: {', '.join(list_suites())}")
    data = json.loads(path.read_text(encoding="utf-8"))
    tasks = []
    for raw in data.get("tasks", []):
        oracles = [Oracle(kind=o["kind"], value=str(o.get("value", "")),
                          weight=float(o.get("weight", 1.0)))
                   for o in raw.get("oracles", [])]
        tasks.append(TaskSpec(
            id=raw["id"], prompt=raw["prompt"], oracles=oracles,
            tags=list(raw.get("tags", [])), expect_tool=raw.get("expect_tool", ""),
            note=raw.get("note", "")))
    return Suite(name=data.get("name", path.stem),
                 description=data.get("description", ""), tasks=tasks)


def list_suites() -> list[str]:
    if not SUITE_DIR.is_dir():
        return []
    return sorted(p.stem for p in SUITE_DIR.glob("*.json"))


class EvalHarness:
    """Runs suites against a live kernel."""

    def __init__(self, jarvis: Any) -> None:
        self.j = jarvis
        self.registry = jarvis.registry

    def _side_effect(self, tool: str) -> str:
        spec = self.registry.get(tool)
        return spec.spec.side_effect.value if spec else "none"

    async def run_task(self, spec: TaskSpec, *, timeout_s: float = 240.0) -> TaskReport:
        session = self.j.session(f"eval-{spec.id}")
        session["messages"] = []                      # fresh context per task

        approvals: list[dict[str, Any]] = []

        def tap(ev) -> None:
            if ev.topic == "approval.requested":
                approvals.append(dict(ev.data))

        self.j.bus.tap(tap)
        traces: list[ToolTrace] = []
        denied: list[str] = []
        final_text = ""
        error: str | None = None
        t0 = time.perf_counter()
        try:
            async for ev in self.j.agent.run_turn(spec.prompt, session=session,
                                                  source="eval"):
                if ev["kind"] == "tool_call":
                    traces.append(ToolTrace(
                        tool=ev.get("tool", ""), args=ev.get("args", {}),
                        side_effect=self._side_effect(ev.get("tool", ""))))
                elif ev["kind"] == "tool_result":
                    summary = str(ev.get("summary", ""))
                    for tr in reversed(traces):
                        if tr.tool == ev.get("tool") and not tr.ok:
                            tr.ok = bool(ev.get("ok"))
                            tr.verified = bool(ev.get("verified"))
                            break
                    if not ev.get("ok") and "denied by policy" in summary:
                        denied.append(str(ev.get("tool")))
                elif ev["kind"] == "final":
                    final_text = ev.get("text", "")
                elif ev["kind"] == "error":
                    error = ev.get("text")
        except Exception as exc:  # a harness bug must not look like a model failure
            error = f"harness error: {type(exc).__name__}: {exc}"
        duration = time.perf_counter() - t0

        result = EvalResult(
            task_id=spec.id, prompt=spec.prompt, final_text=final_text,
            tool_traces=traces, approvals=approvals, denied=denied,
            error=error, duration_s=duration, honorific=self.j.persona.addr)
        outcomes = evaluate(spec.oracles, result)
        return TaskReport(task=spec, result=result, outcomes=outcomes)

    async def run_suite(self, suite: Suite) -> SuiteReport:
        report = SuiteReport(suite=suite.name, started=_now(),
                             model=self.j.config.llm.model)
        # A suite must never hang on an approval prompt; a short timeout turns an
        # unanswered prompt into a denial, which is what safety tasks expect.
        previous_timeout = self.j.config.policy.approval_timeout_s
        self.j.config.policy.approval_timeout_s = 1.5
        try:
            for spec in suite.tasks:
                log.info("eval %s/%s: %s", suite.name, spec.id, spec.prompt[:70])
                report.tasks.append(await self.run_task(spec))
        finally:
            self.j.config.policy.approval_timeout_s = previous_timeout
            report.finished = _now()
        return report

    def write_report(self, report: SuiteReport, directory: Path | None = None) -> Path:
        out_dir = directory or (self.j.config.data_path / "eval-reports")
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = report.started.replace(":", "").replace("-", "").replace("T", "-")
        stem = f"{report.suite}-{stamp}"
        (out_dir / f"{stem}.json").write_text(
            json.dumps(report.to_dict(), indent=2), encoding="utf-8")
        md = out_dir / f"{stem}.md"
        md.write_text(report.to_markdown(), encoding="utf-8")
        return md


def _now() -> str:
    import time as _t
    return _t.strftime("%Y-%m-%dT%H:%M:%S", _t.localtime())
