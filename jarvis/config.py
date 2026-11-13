"""Typed configuration for JARVIS.

Load order (later wins):
  1. built-in defaults (this module)
  2. config/jarvis.toml  (or $JARVIS_CONFIG)
  3. environment variables prefixed JARVIS_

Every value has a safe default so a fresh clone starts without a config file.
Nothing here reads secrets into model context; secrets live behind the broker
in jarvis.policy.secrets.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any

__all__ = ["Config", "load_config", "PROJECT_ROOT", "DATA_DIR"]


def _find_root() -> Path:
    """Project root = the directory containing this package's parent."""
    here = Path(__file__).resolve()
    # .../Jarvis/jarvis/config.py -> .../Jarvis
    return here.parent.parent


PROJECT_ROOT = _find_root()
DATA_DIR = PROJECT_ROOT / "data"


@dataclass
class ServerConfig:
    host: str = "127.0.0.1"
    port: int = 8765
    # Node agent listens here on the laptop; kept separate from SSH (2225).
    node_port: int = 2226
    cors_origins: list[str] = field(default_factory=lambda: ["http://127.0.0.1:8765"])
    # Private-cloud access. Loopback is always trusted; any other client must
    # present the API token (data/api-token) unless its network is listed here.
    allow_lan: bool = False
    trusted_networks: list[str] = field(default_factory=list)   # e.g. ["10.0.0.0/24"]


@dataclass
class LLMConfig:
    provider: str = "ollama"          # ollama | openai_compat | none
    base_url: str = "http://127.0.0.1:11434"
    model: str = "qwen2.5:7b-instruct"
    fast_model: str = ""               # optional smaller model for quick chat
    embed_model: str = "nomic-embed-text"
    temperature: float = 0.4
    top_p: float = 0.9
    num_ctx: int = 6144
    num_predict: int = 512          # cap answer length -> snappier replies
    request_timeout_s: float = 180.0
    keep_alive: str = "30m"
    # Fallback chain used only when the primary is unreachable AND policy allows.
    fallback: list[str] = field(default_factory=list)
    # Optional frontier tier: a remote, high-capability model for hard requests.
    # The API key is never stored here; it is read from the SecretStore under
    # `frontier_key_secret` (or env JARVIS_SECRET_<NAME>). Empty = disabled.
    frontier_provider: str = ""       # anthropic | gemini | groq | "" (off)
    frontier_model: str = ""          # e.g. claude-3-5-sonnet-20241022
    frontier_key_secret: str = ""     # SecretStore key name holding the API key
    # Auto-escalation: a request containing any of these (case-insensitive) is
    # routed to the frontier tier when it is configured. Empty = never escalate
    # (privacy-first default — normal chat always stays local).
    frontier_trigger: list[str] = field(default_factory=list)


@dataclass
class PersonaConfig:
    name: str = "Jarvis"
    address_as: str = "sir"
    formality: float = 0.85       # 0 dry .. 1 very formal
    humor: float = 0.45           # dry wit level
    verbosity: float = 0.3        # 0 terse .. 1 verbose (terser = faster replies)
    british: bool = True
    custom_instructions: str = ""


@dataclass
class VoiceConfig:
    enabled: bool = True
    stt_enabled: bool = True
    tts_enabled: bool = True
    wake_enabled: bool = False
    push_to_talk_hotkey: str = "ctrl+alt+space"
    stt_model: str = "base.en"
    stt_device: str = "cpu"        # cpu | cuda
    stt_compute_type: str = "int8"
    tts_engine: str = "kokoro"     # kokoro | piper | null
    tts_fallback: list[str] = field(default_factory=lambda: ["piper"])
    piper_voice: str = "en_GB-alan-medium"
    piper_dir: str = ""            # defaults to data/voices
    kokoro_voice: str = "bm_george"   # British male: bm_george | bm_lewis | bm_daniel | bm_fable
    kokoro_speed: float = 1.0
    kokoro_dir: str = ""           # defaults to data/voices/kokoro
    # Post-processing that gives the stock voice an AI-butler character.
    effect: str = "jarvis"         # jarvis | none
    effect_strength: float = 0.5   # 0..1
    output: str = "browser"        # browser | server | both — who plays replies
    wake_model: str = "hey_jarvis"
    wake_threshold: float = 0.5   # lower => wakes more easily (more false triggers)
    wake_sound: bool = True       # speak "Yes sir" after the wake word (brief ack)
    vad_threshold: float = 0.02   # RMS energy above which a frame counts as speech
    conversation_timeout_s: float = 8.0   # hands-free: silence that ends a conversation
    # Which hosts voice.speak targets when the caller doesn't name one. "pc",
    # "laptop" and "apos" are the known ids; the workstation ("pc") is the
    # sensible default so spoken replies come out of the machine you're at.
    default_hosts: list[str] = field(default_factory=lambda: ["pc"])
    # Delivery. volume 0..1; length_scale <1 speaks faster (0.9 = 10% quicker);
    # sentence_silence is the gap between sentences in seconds.
    volume: float = 0.65
    length_scale: float = 0.9
    sentence_silence: float = 0.08
    input_device: str = ""         # name substring; empty = system default
    output_device: str = ""
    sample_rate: int = 16000


@dataclass
class MemoryConfig:
    backend: str = "sqlite"
    db_path: str = ""              # defaults to data/jarvis.db
    embed_dim: int = 768
    retrieval_top_k: int = 6
    retention_days: int = 0        # 0 = keep forever (owner-owned)
    private_by_default: bool = False


@dataclass
class KnowledgeConfig:
    enabled: bool = True
    roots: list[str] = field(default_factory=list)   # folders indexed for grounded answers
    extensions: list[str] = field(default_factory=lambda: [
        ".md", ".txt", ".rst", ".py", ".toml", ".json", ".yaml", ".yml", ".ps1", ".sh", ".pdf"])
    chunk_chars: int = 1200
    max_file_mb: float = 5.0


@dataclass
class IntegrationsConfig:
    # Free/self-hosted integrations; each is off until configured.
    home_assistant_url: str = ""   # token: secret "home_assistant"
    weather_location: str = ""     # default place for weather.* (Open-Meteo, no key)
    smtp_host: str = ""            # email drafts always work; sending needs this + secret "smtp"
    smtp_port: int = 587
    smtp_user: str = ""


@dataclass
class PolicyConfig:
    mode: str = "ask"              # ask | autonomous | readonly
    auto_allow: list[str] = field(
        default_factory=lambda: [
            "system.info", "system.metrics", "time.now",
            "calc.eval", "memory.search", "memory.recall",
            "web.search", "web.fetch", "notes.list", "notes.read",
            "files.read", "files.list", "nodes.status",
            "discovery.scan", "monitoring.status", "sandbox.status",
            "anton.status", "anton.jobs", "anton.nodes", "anton.routines",
            "vision.ocr", "vision.screen", "vision.live",
            "code.read", "code.search", "code.symbols", "code.diff", "git.info",
            "monitor.probe", "sandbox.remote_status",
            "evolve.symbolic", "evolve.minimize", "evolve.selftune", "evolve.status",
            "evolve.tune", "evolve.auto", "evolve.self",
            "evolve.train", "evolve.get_status", "evolve.cancel",
            "evolve.list_models", "evolve.evaluate_model", "evolve.compare",
            "evolve.mission.submit", "evolve.mission.status",
            "evolve.mission.resume",
        ]
    )
    approval_timeout_s: float = 30.0
    always_deny: list[str] = field(default_factory=list)


@dataclass
class MonitoringConfig:
    enabled: bool = True
    interval_s: float = 15.0
    cpu_warn: float = 90.0
    mem_warn: float = 90.0
    disk_warn: float = 90.0
    temp_warn_c: float = 85.0
    alert_cooldown_s: float = 600.0
    quiet_hours: list[str] = field(default_factory=lambda: ["23:30-07:00"])
    # Also poll each enrolled node's live metrics on the sample interval and
    # store them as node.<id>.<metric> rows, so the HUD shows every machine.
    poll_nodes: bool = True


@dataclass
class NightConfig:
    enabled: bool = False
    start: str = "01:00"
    end: str = "06:00"
    max_minutes: int = 240
    tasks: list[str] = field(default_factory=list)
    report_dir: str = ""           # defaults to data/night-reports


@dataclass
class NodesConfig:
    enabled: bool = True
    token: str = ""                        # shared secret for node auth; generated if empty
    known: dict[str, str] = field(default_factory=dict)   # node_id -> base_url
    ssh: dict[str, dict[str, Any]] = field(default_factory=dict)


@dataclass
class SecurityConfig:
    audit_enabled: bool = True
    bind_local_only: bool = True
    # DEFCON posture 5 (peace) .. 1 (max). Affects autonomy and monitoring.
    defcon: int = 5
    # web.fetch is auto-allowed. Refuse loopback/private/link-local targets by
    # default so a prompt-injected page cannot use it as an SSRF proxy to the
    # Core API or a router's admin page. Set true only for deliberate LAN use.
    allow_private_fetch: bool = False


@dataclass
class GoalsConfig:
    """Durable multi-step planning and execution.

    A goal is decomposed once by the planner model, then executed step by step by
    the runtime. Each step still goes through the task controller, so an approval
    or a destructive action is gated exactly as a spoken request.
    """
    enabled: bool = True
    max_steps: int = 8
    max_attempts: int = 3
    auto_run: bool = True          # the runtime advances planned goals
    per_tick: int = 1              # steps advanced per runtime tick
    synthesize_answers: bool = True  # fold step results into an answer with a placeholder


@dataclass
class OpsConfig:
    """Self-diagnostics and narrow, audited repairs."""
    enabled: bool = True
    interval_s: float = 60.0
    auto_repair_tunnel: bool = True
    repair_cooldown_s: float = 120.0


@dataclass
class RuntimeConfig:
    """The persistent runtime: the tick loop that makes JARVIS operate, not just answer.

    ``tick_s`` is how often the loop looks for due jobs and pending event
    reactions. ``lease_ttl_s`` bounds how long one Core owns the loop, so a
    crashed Core is taken over and two Cores never both fire a job (the
    scheduler's per-occurrence claim is the hard guarantee). ``catch_up_s`` is
    how late a missed job may still fire; later than that it is recorded as
    ``missed`` instead of surprising the owner hours afterwards.
    ``event_topics`` mirrors these bus topics into the event log so rules can
    react; leave it narrow to avoid a firehose.
    """
    enabled: bool = True
    tick_s: float = 2.0
    lease_ttl_s: float = 30.0
    max_jobs_per_tick: int = 5
    max_jobs_in_flight: int = 2
    max_reactions_per_tick: int = 10
    max_reaction_tasks: int = 4
    catch_up_s: float = 3600.0
    session_max_messages: int = 20   # bound the prompt; oldest exchanges drop first
    rule_cooldown_s: float = 5.0
    event_retention_days: int = 30
    max_events: int = 5000
    event_topics: list[str] = field(default_factory=lambda: [
        "alert", "security.*", "honeypot.*",
    ])


@dataclass
class SandboxConfig:
    """Containment for process runs (shell.run, desktop tools).

    tier: auto | jobobject | rlimit | none. ``auto`` picks the best the platform
    offers (a Windows Job Object on Windows, POSIX rlimits elsewhere). Memory,
    process-count and CPU-time caps are enforced by those tiers; filesystem and
    network isolation need a VM/container and are not integrated (reported, not
    implied). ``scrub_env`` strips JARVIS_SECRET_* and stored-secret values from
    every child's environment.
    """
    enabled: bool = True
    tier: str = "auto"
    max_memory_mb: int = 2048          # per-process memory ceiling (0 = unlimited)
    max_processes: int = 32            # simultaneous processes in the tree (0 = unlimited)
    max_cpu_seconds: int = 0           # CPU-time ceiling (0 = unlimited)
    max_file_mb: int = 0               # largest file the child may write (POSIX; 0 = unlimited)
    wall_timeout_s: float = 30.0       # default wall-clock timeout
    kill_tree: bool = True             # kill the whole process tree, not just the child
    scrub_env: bool = True             # hand children a secret-free environment
    # Remote code runner (sandbox.run_code): a separate machine reached through
    # the operator's SSH config. Limits are kernel-enforced on the runner.
    remote_host: str = "laptop"        # SSH host alias, not an address or secret
    remote_connect_timeout_s: float = 8.0


@dataclass
class Config:
    core_name: str = "jarvis-core"
    host_role: str = "core"        # core | node
    log_level: str = "INFO"
    data_dir: str = ""
    server: ServerConfig = field(default_factory=ServerConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    persona: PersonaConfig = field(default_factory=PersonaConfig)
    voice: VoiceConfig = field(default_factory=VoiceConfig)
    memory: MemoryConfig = field(default_factory=MemoryConfig)
    knowledge: KnowledgeConfig = field(default_factory=KnowledgeConfig)
    integrations: IntegrationsConfig = field(default_factory=IntegrationsConfig)
    policy: PolicyConfig = field(default_factory=PolicyConfig)
    monitoring: MonitoringConfig = field(default_factory=MonitoringConfig)
    night: NightConfig = field(default_factory=NightConfig)
    nodes: NodesConfig = field(default_factory=NodesConfig)
    security: SecurityConfig = field(default_factory=SecurityConfig)
    sandbox: SandboxConfig = field(default_factory=SandboxConfig)
    runtime: RuntimeConfig = field(default_factory=RuntimeConfig)
    goals: GoalsConfig = field(default_factory=GoalsConfig)
    ops: OpsConfig = field(default_factory=OpsConfig)

    # ---- derived paths -------------------------------------------------
    @property
    def data_path(self) -> Path:
        return Path(self.data_dir) if self.data_dir else DATA_DIR

    @property
    def db_path(self) -> Path:
        return Path(self.memory.db_path) if self.memory.db_path else self.data_path / "jarvis.db"

    @property
    def voices_path(self) -> Path:
        return Path(self.voice.piper_dir) if self.voice.piper_dir else self.data_path / "voices"

    @property
    def plugins_path(self) -> Path:
        return self.data_path / "plugins"

    @property
    def kokoro_path(self) -> Path:
        return Path(self.voice.kokoro_dir) if self.voice.kokoro_dir else self.voices_path / "kokoro"

    @property
    def night_report_path(self) -> Path:
        return Path(self.night.report_dir) if self.night.report_dir else self.data_path / "night-reports"

    def ensure_dirs(self) -> None:
        for p in (self.data_path, self.voices_path, self.night_report_path,
                  self.data_path / "logs", self.data_path / "artifacts", self.plugins_path):
            p.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# loading helpers
# ---------------------------------------------------------------------------

def _coerce(value: Any, target: Any) -> Any:
    """Best-effort coercion from TOML/env values into the field's type."""
    if target is float:
        return float(value)
    if target is int and not isinstance(target, bool):
        return int(value)
    if target is bool:
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in {"1", "true", "yes", "on"}
    if target is str:
        return str(value)
    return value


def _as_list(value: Any) -> list[Any]:
    """Coerce a config value into a list (a TOML/env string is comma-separated)."""
    if isinstance(value, list):
        return value
    if isinstance(value, (tuple, set)):
        return list(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        return [part.strip() for part in text.split(",") if part.strip()]
    return [value]


def _apply_table(obj: Any, table: dict[str, Any]) -> None:
    valid = {f.name: f for f in fields(obj)}
    for key, raw in table.items():
        if key not in valid:
            continue
        f = valid[key]
        current = getattr(obj, key)
        # nested dataclass
        if is_dataclass(current) and isinstance(raw, dict):
            _apply_table(current, raw)
            continue
        # list-valued fields accept a list or a comma-separated string
        if isinstance(current, list):
            setattr(obj, key, _as_list(raw))
            continue
        # dict-typed fields declared with default_factory
        if isinstance(current, dict):
            setattr(obj, key, raw)
            continue
        try:
            setattr(obj, key, _coerce(raw, f.type if not isinstance(f.type, str) else type(current)))
        except (TypeError, ValueError):
            setattr(obj, key, raw)


def _apply_env(cfg: Config) -> None:
    """JARVIS_<SECTION>__<FIELD> or JARVIS_<FIELD> environment overrides."""
    for name, value in os.environ.items():
        if not name.startswith("JARVIS_"):
            continue
        parts = name[len("JARVIS_"):].lower().split("__")
        if len(parts) == 1:
            if hasattr(cfg, parts[0]):
                _apply_table_scalar(cfg, parts[0], value)
            continue
        section, _, attr = parts[0], parts[1], "__".join(parts[1:])
        if hasattr(cfg, section):
            sub = getattr(cfg, section)
            if is_dataclass(sub) and hasattr(sub, attr):
                _apply_table_scalar(sub, attr, value)


def _apply_table_scalar(obj: Any, attr: str, value: str) -> None:
    current = getattr(obj, attr)
    if isinstance(current, list):
        setattr(obj, attr, _as_list(value))
        return
    try:
        setattr(obj, attr, _coerce(value, type(current)))
    except (TypeError, ValueError):
        setattr(obj, attr, value)


def load_config(path: str | os.PathLike[str] | None = None) -> Config:
    cfg = Config()
    candidates: list[Path] = []
    if path:
        candidates.append(Path(path))
    elif os.environ.get("JARVIS_CONFIG"):
        candidates.append(Path(os.environ["JARVIS_CONFIG"]))
    else:
        candidates.append(PROJECT_ROOT / "config" / "jarvis.toml")
        candidates.append(PROJECT_ROOT / "config" / "jarvis.example.toml")

    for candidate in candidates:
        if candidate and candidate.is_file():
            raw = candidate.read_bytes()
            # Windows editors (Notepad, PowerShell 5.1) may prepend a UTF-8 BOM;
            # tomllib rejects it, so strip it rather than refusing to start.
            if raw.startswith(b"\xef\xbb\xbf"):
                raw = raw[3:]
            data = tomllib.loads(raw.decode("utf-8"))
            _apply_table(cfg, data)
            break

    _apply_env(cfg)
    return cfg
