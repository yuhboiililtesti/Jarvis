"""JARVIS command line.

    python -m jarvis serve          # run the Core (API + HUD) in the foreground
    python -m jarvis chat "..."     # one-shot text turn
    python -m jarvis repl           # interactive text loop
    python -m jarvis tools          # list the capability surface
    python -m jarvis health         # subsystem health
    python -m jarvis sandbox        # process-containment posture (--check to probe)
    python -m jarvis audit          # verify the tamper-evident audit chain
    python -m jarvis runtime        # persistent runtime: status | tick | rules | events
    python -m jarvis goals          # durable multi-step goals: list | show | create | run | cancel
    python -m jarvis ops            # self-diagnostics: status | check
    python -m jarvis mcp            # stdio MCP server for other agents (read-only by default)
    python -m jarvis doctor         # environment + dependency diagnosis
    python -m jarvis init           # create config/ and data/ with defaults
    python -m jarvis night "..."    # run the night protocol now
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from .config import PROJECT_ROOT, load_config
from .version import banner


def _build(cfg=None):
    from .core import build
    return build(cfg)


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn
    from .api import create_app

    cfg = load_config(args.config)
    if args.host:
        cfg.server.host = args.host
    if args.port:
        cfg.server.port = args.port
    if args.lan:
        # Private-cloud mode: listen on every interface; the auth gate then
        # requires the API token from anything that is not this machine.
        cfg.server.allow_lan = True
        if not args.host:
            cfg.server.host = "0.0.0.0"
    elif cfg.server.host not in ("127.0.0.1", "localhost", "::1") and not cfg.server.allow_lan:
        print(f"refusing to bind {cfg.server.host}: set server.allow_lan or pass --lan")
        return 2
    print(banner())
    print(f"HUD: http://{'127.0.0.1' if cfg.server.host == '0.0.0.0' else cfg.server.host}"
          f":{cfg.server.port}/")
    if cfg.server.allow_lan:
        print("LAN access ON: remote clients need the API token (jarvis token) or pairing.")
        print("WARNING: this is plain HTTP. The token/cookie crosses the LAN in clear text; "
              "use a TLS reverse proxy (or WireGuard/Tailscale) for untrusted networks.")
    app = create_app(_build(cfg))
    uvicorn.run(app, host=cfg.server.host, port=cfg.server.port, log_level="info")
    return 0


def cmd_chat(args: argparse.Namespace) -> int:
    j = _build(load_config(args.config))

    async def run() -> int:
        try:
            text = await j.agent.complete(args.message, session=j.session("cli"))
        finally:
            await j.stop()
        print(text)
        return 0

    return asyncio.run(run())


def cmd_repl(args: argparse.Namespace) -> int:
    j = _build(load_config(args.config))
    print(banner())
    print(f"Type a request, or 'exit'. Mode: {j.config.policy.mode}. "
          f"Tools: {len(j.registry.names())}. Ctrl+C to quit.")
    session = j.session("cli")

    async def loop() -> int:
        while True:
            try:
                line = input("\nyou> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if line.lower() in {"exit", "quit", ":q"}:
                break
            if not line:
                continue
            print("jarvis> ", end="", flush=True)
            try:
                async for ev in j.agent.run_turn(line, session=session, source="text"):
                    if ev["kind"] == "token":
                        print(ev["text"], end="", flush=True)
                    elif ev["kind"] == "tool_call":
                        print(f"\n  [tool] {ev['tool']} {json.dumps(ev.get('args', {}))[:160]}",
                              flush=True)
                    elif ev["kind"] == "tool_result":
                        mark = "ok" if ev["ok"] else "FAIL"
                        verified = "verified" if ev.get("verified") else "unverified"
                        print(f"  [{mark}/{verified}] {str(ev.get('summary'))[:160]}", flush=True)
                    elif ev["kind"] == "error":
                        print(f"\n  [error] {ev['text']}", flush=True)
                print()
            except Exception as exc:
                print(f"\n  [error] {exc}")
        await j.stop()
        return 0

    return asyncio.run(loop())


def cmd_tools(args: argparse.Namespace) -> int:
    j = _build(load_config(args.config))
    if args.health:
        health = j.registry.health()
        bad = 0
        for name in sorted(health):
            h = health[name]
            bad += not h["available"]
            mark = "ok  " if h["available"] else "DOWN"
            print(f"  [{mark}] {name:<26} {h['source']:<16} {h.get('reason', '')}")
        print(f"\n{len(health) - bad}/{len(health)} available")
        return 0
    cats = j.registry.by_category()
    print(f"{banner()}\n")
    print(f"{len(j.registry.names())} tools across {len(cats)} categories "
          f"(mode: {j.policy.effective_mode})\n")
    for cat in sorted(cats):
        print(f"  {cat}")
        for spec in sorted(cats[cat], key=lambda s: s.name):
            auto = " [auto]" if spec.side_effect.value == "none" else ""
            print(f"    {spec.name:<26} v{spec.version:<5} {spec.side_effect.value:<12}{auto}  "
                  f"{spec.summary}")
    return 0


def cmd_health(args: argparse.Namespace) -> int:
    j = _build(load_config(args.config))

    async def run() -> int:
        try:
            h = await j.health()
        finally:
            await j.stop()
        print(json.dumps(h, indent=2, default=str))
        return 0

    return asyncio.run(run())


def cmd_doctor(args: argparse.Namespace) -> int:
    import importlib.util
    import platform
    import shutil

    print(banner())
    print(f"python   {sys.version.split()[0]}  ({platform.platform()})")
    print(f"project  {PROJECT_ROOT}")
    cfg = load_config(args.config)
    print(f"data     {cfg.data_path}")
    print()
    checks = [
        ("fastapi", "fastapi", "Core API"),
        ("uvicorn", "uvicorn", "Core API"),
        ("psutil", "psutil", "system metrics"),
        ("httpx", "httpx", "LLM/network"),
        ("faster_whisper", "faster_whisper", "speech-to-text (optional)"),
        ("sounddevice", "sounddevice", "audio I/O (optional)"),
        ("openwakeword", "openwakeword", "wake word (optional)"),
        ("kokoro_onnx", "kokoro_onnx", "text-to-speech, JARVIS voice (optional)"),
        ("piper", "piper", "text-to-speech fallback (optional)"),
        ("numpy", "numpy", "voice math (optional)"),
        ("sympy", "sympy", "compute engine (optional)"),
        ("PIL", "PIL", "screen capture (optional)"),
        ("rapidocr_onnxruntime", "rapidocr_onnxruntime", "local OCR / vision (optional)"),
    ]
    for mod, import_name, purpose in checks:
        ok = importlib.util.find_spec(import_name) is not None
        print(f"  [{'x' if ok else ' '}] {mod:<16} {purpose}")
    print()
    for exe, purpose in [("ollama", "local model runtime"),
                         ("ffmpeg", "audio conversion"),
                         ("git", "version control")]:
        found = shutil.which(exe)
        print(f"  [{'x' if found else ' '}] {exe:<16} {found or 'MISSING'}  ({purpose})")
    print()
    try:
        import httpx  # noqa: F401
        r = None
        import urllib.request
        with urllib.request.urlopen(cfg.llm.base_url + "/api/tags", timeout=3) as resp:
            r = json.loads(resp.read().decode())
        models = [m["name"] for m in r.get("models", [])]
        if models:
            print(f"  [x] ollama models: {', '.join(models)}")
        else:
            print("  [ ] ollama reachable but NO models pulled")
            print(f"      run:  ollama pull {cfg.llm.model}")
    except Exception as exc:
        print(f"  [ ] ollama not reachable at {cfg.llm.base_url} ({exc})")
    print("\nDoctor is informational; it changes nothing.")
    return 0


def cmd_init(args: argparse.Namespace) -> int:
    cfgdir = PROJECT_ROOT / "config"
    cfgdir.mkdir(parents=True, exist_ok=True)
    target = cfgdir / "jarvis.toml"
    example = cfgdir / "jarvis.example.toml"
    if target.exists() and not args.force:
        print(f"{target} already exists (use --force to overwrite)")
    else:
        target.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
        print(f"wrote {target}")
    cfg = load_config(target)
    cfg.ensure_dirs()
    print(f"data dirs ready under {cfg.data_path}")
    return 0


def cmd_sandbox(args: argparse.Namespace) -> int:
    """Show — and optionally probe — how child processes are contained."""
    j = _build(load_config(args.config))
    try:
        svc = j.services.get("sandbox")
        if svc is None:
            print("sandbox service not loaded")
            return 1
        status = svc.status()
        print(f"{banner()}\n")
        print(f"sandbox      active tier '{status['active_tier']}' "
              f"(requested '{status['requested_tier']}', enabled={status['enabled']})")
        print(f"contained    {status['contained']}")
        print(f"env scrubbed {status['env_scrubbed']}")
        lim = status["limits"]
        cpu = f"{lim['max_cpu_seconds']}s" if lim["max_cpu_seconds"] else "unlimited"
        print(f"limits       mem {lim['max_memory_mb']} MiB | procs {lim['max_processes']} | "
              f"cpu {cpu} | wall {lim['wall_timeout_s']}s")
        print("\ntiers:")
        for t in status["tiers"]:
            mark = "ok  " if t["available"] else "n/a "
            print(f"  [{mark}] {t['tier']:<16} {t['reason']}")
        print("\nisolation:")
        for key, val in status["isolation"].items():
            print(f"  {key:<20} {val}")
        if args.check:
            print("\nprobe:")
            r = svc.run([sys.executable, "-c", "print('sandbox-probe-ok')"], timeout=20)
            print(f"  tier={r.tier} contained={r.contained} exit={r.exit_code} "
                  f"stdout={r.stdout.strip()!r}")
            verdict = "enforced" if r.contained else "NOT enforced (uncontained)"
            print(f"  OS containment: {verdict}")
            return 0 if r.ok and r.contained else 1
        return 0
    finally:
        j.db.close()


def cmd_audit(args: argparse.Namespace) -> int:
    """Verify the tamper-evident audit chain against its external anchor."""
    from .db import Database
    from .security.audit_chain import anchor_path, verify

    cfg = load_config(args.config)
    cfg.ensure_dirs()
    db = Database(cfg.db_path)
    try:
        if args.action == "head":
            print(json.dumps(db.audit_head() or {"id": 0, "hash": None}, indent=2))
            return 0
        if args.action == "anchor":
            print(json.dumps(db.write_anchor(anchor_path(cfg.data_path)), indent=2))
            return 0
        res = verify(db, cfg.data_path)
        ok = bool(res.get("ok"))
        print(f"audit chain  {'OK' if ok else 'BROKEN'}")
        print(f"checked      {res.get('checked')} rows ({res.get('legacy')} legacy, unchained)")
        print(f"anchored     {res.get('anchored')}  ({res.get('anchor_file')})")
        if not ok:
            print(f"reason       {res.get('reason')} (at row {res.get('broken_at')})")
        print(f"head         {res.get('head')}")
        return 0 if ok else 1
    finally:
        db.close()


def cmd_plugins(args: argparse.Namespace) -> int:
    """Inspect, approve or withdraw plugins. Approval pins the files' hash."""
    from .db import Database
    from .plugins import PluginManager

    cfg = load_config(args.config)
    cfg.ensure_dirs()
    db = Database(cfg.db_path)
    try:
        mgr = PluginManager(cfg.plugins_path, db)
        if args.action == "enable":
            if not args.plugin_id:
                print("usage: jarvis plugins enable <id>")
                return 2
            mgr.discover()
            info = mgr.plugins.get(args.plugin_id)
            if info is None:
                print(f"no plugin '{args.plugin_id}' in {cfg.plugins_path}")
                return 1
            print(f"plugin   {info.id} {info.version} — {info.summary}")
            print(f"modules  {', '.join(info.modules)}")
            print(f"ceiling  {info.max_side_effect} (no tool may exceed this)")
            print(f"sha256   {info.sha256}")
            print("Plugin code runs inside the Core with its rights. Enable only code you have read.")
            if not args.yes and input("Approve these exact files? [y/N] ").strip().lower() != "y":
                print("not enabled")
                return 1
            try:
                mgr.enable(args.plugin_id)
            except (KeyError, ValueError) as exc:
                print(exc)
                return 1
            print("enabled; restart the Core to load it")
            return 0
        if args.action == "disable":
            ok = mgr.disable(args.plugin_id or "")
            print("disabled; restart the Core to unload it" if ok else "was not enabled")
            return 0 if ok else 1
        infos = mgr.discover()
        if not infos:
            print(f"no plugins in {cfg.plugins_path} (see docs/EXTENDING.md)")
        for info in infos:
            print(f"  {info.id:<20} {info.version:<8} {info.state:<9} "
                  f"ceiling={info.max_side_effect:<12} {info.reason}")
        return 0
    finally:
        db.close()


def cmd_runtime(args: argparse.Namespace) -> int:
    """Inspect or drive the persistent runtime: status, one tick, rules, events."""
    j = _build(load_config(args.config))
    try:
        svc = j.services.get("runtime")
        if svc is None:
            print("runtime service not loaded")
            return 1
        action = args.action or "status"

        if action == "status":
            print(json.dumps(svc.status(), indent=2, default=str))
            return 0

        if action == "tick":
            async def one_tick():
                # A one-shot process must not claim a job it cannot finish, so
                # scheduled jobs are opt-in here; the running Core owns them.
                res = await svc.tick(jobs=bool(getattr(args, "jobs", False)))
                await svc.drain()
                await svc.stop()          # release the lease immediately
                return res
            print(json.dumps(asyncio.run(one_tick()), indent=2, default=str))
            return 0

        if action == "rules":
            rule_action = args.rule_action or "list"
            if rule_action == "list":
                print(json.dumps(svc.events.list_rules(), indent=2, default=str))
                return 0
            if rule_action == "add":
                if not (args.name and args.topic):
                    print("usage: jarvis runtime rules add <name> --topic T "
                          "(--tool TOOL [--args JSON] | --notify TITLE [--message M] | "
                          "--request PROMPT) [--condition JSON]")
                    return 2
                if args.tool:
                    action_obj = {"kind": "tool", "tool": args.tool,
                                  "args": json.loads(args.args or "{}")}
                elif args.notify:
                    action_obj = {"kind": "notify", "title": args.notify,
                                  "message": args.message or ""}
                elif args.request:
                    action_obj = {"kind": "request", "prompt": args.request}
                else:
                    print("a rule needs --tool, --notify or --request")
                    return 2
                cond = json.loads(args.condition) if args.condition else None
                try:
                    rule = svc.events.add_rule(args.name, args.topic, action_obj, cond)
                except ValueError as exc:
                    print(exc)
                    return 1
                print(json.dumps(rule, indent=2, default=str))
                return 0
            if rule_action == "remove":
                ok = svc.events.remove_rule(args.name or "")
                print("removed" if ok else "no such rule")
                return 0 if ok else 1
            print(f"unknown rules action '{rule_action}'")
            return 2

        if action == "events":
            if args.emit:
                try:
                    data = json.loads(args.data or "{}")
                except json.JSONDecodeError as exc:
                    print(f"--data is not valid JSON: {exc}")
                    return 2
                res = svc.events.ingest(args.emit, data, source="cli")
                print(json.dumps(res, indent=2, default=str))

                async def react():
                    for fire in svc.events.pending():
                        await svc.events.execute_fire(fire)
                asyncio.run(react())
                return 0
            print(json.dumps(svc.events.status(), indent=2, default=str))
            return 0

        print(f"unknown runtime action '{action}'")
        return 2
    finally:
        j.db.close()


def cmd_mcp(args: argparse.Namespace) -> int:
    """Run a stdio MCP server so another agent can use JARVIS's tools."""
    from .mcp import run_stdio

    j = _build(load_config(args.config))
    try:
        asyncio.run(run_stdio(j, allow_mutations=bool(args.allow_mutations)))
    except KeyboardInterrupt:  # pragma: no cover - interactive
        pass
    finally:
        j.db.close()
    return 0


def cmd_ops(args: argparse.Namespace) -> int:
    """Show JARVIS's own diagnostics, optionally running a fresh check."""
    j = _build(load_config(args.config))
    try:
        svc = j.services.get("ops")
        if svc is None:
            print("ops service not loaded")
            return 1
        action = args.action or "status"
        if action == "check":
            print(json.dumps(asyncio.run(svc.check()), indent=2, default=str))
        else:
            print(json.dumps(svc.status(), indent=2, default=str))
        return 0
    finally:
        j.db.close()


def cmd_goals(args: argparse.Namespace) -> int:
    """Plan and run durable multi-step goals."""
    j = _build(load_config(args.config))
    try:
        svc = j.services.get("goals")
        if svc is None:
            print("goals service not loaded")
            return 1
        action = args.action or "list"

        if action == "list":
            print(json.dumps(svc.list_goals(args.limit or 20), indent=2, default=str))
            return 0

        if action == "show":
            if not args.goal_id:
                print("usage: jarvis goals show <goal_id>")
                return 2
            goal = svc.status(args.goal_id)
            if goal.get("status") == "missing":
                print(f"no goal '{args.goal_id}'")
                return 1
            print(json.dumps(goal, indent=2, default=str))
            return 0

        if action == "create":
            if not args.request:
                print("usage: jarvis goals create --request \"...\" [--plan-only]")
                return 2

            async def plan():
                steps = await svc.plan(args.request)
                return {"plan": steps} if args.plan_only else svc.create(
                    args.request, steps=steps)

            print(json.dumps(asyncio.run(plan()), indent=2, default=str))
            return 0

        if action == "run":
            if not args.goal_id:
                print("usage: jarvis goals run <goal_id>")
                return 2
            print(json.dumps(asyncio.run(svc.advance(args.goal_id)),
                             indent=2, default=str))
            return 0

        if action == "cancel":
            ok = svc.cancel(args.goal_id or "")
            print("cancelled" if ok else "not cancellable (missing or finished)")
            return 0 if ok else 1

        print(f"unknown goals action '{action}'")
        return 2
    finally:
        j.db.close()


def cmd_night(args: argparse.Namespace) -> int:
    j = _build(load_config(args.config))

    async def run() -> int:
        svc = j.services.get("night")
        if svc is None:
            print("night runner not available (module missing)")
            await j.stop()
            return 1
        try:
            run_id = await svc.run_now([args.objective] if args.objective else None)
        finally:
            await j.stop()
        print(f"night run {run_id} complete; report under {j.config.night_report_path}")
        return 0

    return asyncio.run(run())


def cmd_eval(args: argparse.Namespace) -> int:
    from .eval import EvalHarness, list_suites, load_suite

    if args.list:
        names = list_suites()
        print("available suites:", ", ".join(names) if names else "(none)")
        return 0

    name = args.suite or "core"
    try:
        suite = load_suite(name)
    except FileNotFoundError as exc:
        print(exc)
        return 2
    print(f"{banner()}\nsuite '{suite.name}': {len(suite.tasks)} tasks — {suite.description}")

    j = _build(load_config(args.config))

    async def run() -> int:
        harness = EvalHarness(j)
        try:
            report = await harness.run_suite(suite)
            path = harness.write_report(report)
        finally:
            await j.stop()
        print(f"\n{report.passed}/{report.total} passed ({report.score:.0%})")
        for t in report.tasks:
            mark = "PASS" if t.passed else "FAIL"
            tools = ", ".join(tr.tool for tr in t.result.tool_traces) or "-"
            print(f"  [{mark}] {t.task.id:26} {t.result.duration_s:6.1f}s  tools: {tools}")
            for o in t.outcomes:
                if not o.passed:
                    print(f"           ! {o.kind}: {o.detail}")
        print(f"\nreport: {path}")
        return 0 if report.passed == report.total else 1

    return asyncio.run(run())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="jarvis", description="JARVIS local assistant")
    parser.add_argument("--config", default=None, help="path to jarvis.toml")
    parser.add_argument("--version", action="store_true", help="print version and exit")
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("serve", help="run the Core API + HUD")
    p.add_argument("--host")
    p.add_argument("--port", type=int)
    p.add_argument("--lan", action="store_true",
                   help="private-cloud mode: serve the LAN behind the API token")
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("chat", help="one-shot text turn")
    p.add_argument("message")
    p.set_defaults(func=cmd_chat)

    p = sub.add_parser("repl", help="interactive text loop")
    p.set_defaults(func=cmd_repl)

    p = sub.add_parser("tools", help="list tools")
    p.add_argument("--health", action="store_true", help="probe each tool's availability")
    p.set_defaults(func=cmd_tools)

    p = sub.add_parser("plugins", help="list, enable or disable owner-approved plugins")
    p.add_argument("action", nargs="?", default="list", choices=("list", "enable", "disable"))
    p.add_argument("plugin_id", nargs="?")
    p.add_argument("--yes", action="store_true", help="approve without the prompt")
    p.set_defaults(func=cmd_plugins)

    p = sub.add_parser("sandbox", help="show how child processes are contained")
    p.add_argument("--check", action="store_true",
                   help="run a contained probe process and report the result")
    p.set_defaults(func=cmd_sandbox)

    p = sub.add_parser("audit", help="verify the tamper-evident audit chain")
    p.add_argument("action", nargs="?", default="verify",
                   choices=("verify", "head", "anchor"))
    p.set_defaults(func=cmd_audit)

    p = sub.add_parser("runtime", help="persistent runtime: status, tick, rules, events")
    p.add_argument("action", nargs="?", default="status",
                   choices=("status", "tick", "rules", "events"))
    p.add_argument("rule_action", nargs="?", choices=("list", "add", "remove"))
    p.add_argument("name", nargs="?", help="rule name (rules add/remove)")
    p.add_argument("--topic")
    p.add_argument("--tool")
    p.add_argument("--args")
    p.add_argument("--notify")
    p.add_argument("--message")
    p.add_argument("--request")
    p.add_argument("--condition")
    p.add_argument("--emit", help="emit an event topic now")
    p.add_argument("--data", help="JSON payload for --emit")
    p.add_argument("--jobs", action="store_true",
                   help="tick: also fire due scheduled jobs (a running Core normally owns them; "
                        "approvals cannot be answered from a one-shot tick)")
    p.set_defaults(func=cmd_runtime)

    p = sub.add_parser("goals", help="plan and run durable multi-step goals")
    p.add_argument("action", nargs="?", default="list",
                   choices=("list", "show", "create", "run", "cancel"))
    p.add_argument("goal_id", nargs="?")
    p.add_argument("--request", help="the objective (for create)")
    p.add_argument("--limit", type=int, help="maximum goals to list")
    p.add_argument("--plan-only", action="store_true",
                   help="create: show the plan without persisting or running it")
    p.set_defaults(func=cmd_goals)

    p = sub.add_parser("ops", help="self-diagnostics and safe repairs")
    p.add_argument("action", nargs="?", default="status", choices=("status", "check"))
    p.set_defaults(func=cmd_ops)

    p = sub.add_parser("mcp", help="run a stdio MCP server exposing JARVIS tools")
    p.add_argument("--allow-mutations", action="store_true",
                   help="also list mutating tools (MCP has no approval channel, so they still refuse)")
    p.set_defaults(func=cmd_mcp)

    sub.add_parser("health", help="subsystem health").set_defaults(func=cmd_health)
    sub.add_parser("doctor", help="environment diagnosis").set_defaults(func=cmd_doctor)

    p = sub.add_parser("init", help="write config/jarvis.toml and data dirs")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("night", help="run the night protocol now")
    p.add_argument("objective", nargs="?", default=None)
    p.set_defaults(func=cmd_night)

    p = sub.add_parser("eval", help="run a held-out evaluation suite")
    p.add_argument("suite", nargs="?", default="core")
    p.add_argument("--list", action="store_true", help="list available suites")
    p.set_defaults(func=cmd_eval)

    args = parser.parse_args(argv)
    if args.version:
        print(banner())
        return 0
    if not getattr(args, "func", None):
        parser.print_help()
        return 0
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
