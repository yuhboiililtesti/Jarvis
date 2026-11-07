"""Full native JARVIS desktop client (Tkinter, no webview/dashboard).

Tabs: Command, Hosts, Tasks, Approvals, Systems, Settings.
Talks to the APOS Core at http://127.0.0.1:8765 through the PC loopback tunnel.

Run with pythonw.exe for no console:
  pythonw.exe scripts\\jarvis-native.py
"""

from __future__ import annotations

import json
import tempfile
import threading
import time
import tkinter as tk
import wave
from pathlib import Path
from tkinter import ttk
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

BASE = "http://127.0.0.1:8765"
SESSION = "native"
ROOT = Path(__file__).resolve().parent.parent
PIPER_MODEL = ROOT / "data" / "voices" / "en_GB-alan-medium.onnx"
PIPER_CONFIG = ROOT / "data" / "voices" / "en_GB-alan-medium.onnx.json"

BG = "#070b10"
PANEL = "#0c131b"
PANEL2 = "#101b25"
FG = "#d8e6f3"
MUTED = "#7890a6"
ACCENT = "#36c6ff"
OK = "#48d597"
WARN = "#f5b74b"
ERR = "#ff6b6b"


def get_json(path: str) -> dict:
    with urlopen(Request(BASE + path, headers={"Accept": "application/json"}), timeout=12) as r:
        return json.loads(r.read().decode())


def post_json(path: str, payload: dict, timeout: float = 20.0) -> dict:
    req = Request(BASE + path, data=json.dumps(payload).encode(), method="POST",
                  headers={"Content-Type": "application/json", "Accept": "application/json"})
    with urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


class VoiceIO:
    """Local Piper TTS + faster-whisper push-to-talk for the native client."""

    def __init__(self, root: tk.Tk, state_cb) -> None:
        self.root = root
        self.state_cb = state_cb
        self.enabled = True
        self._piper = None
        self._whisper = None
        self._lock = threading.Lock()

    def _load_piper(self):
        if self._piper is None:
            from piper import PiperVoice
            self._piper = PiperVoice.load(str(PIPER_MODEL), config_path=str(PIPER_CONFIG))
        return self._piper

    def _load_whisper(self):
        if self._whisper is None:
            from faster_whisper import WhisperModel
            self._whisper = WhisperModel("base.en", device="cpu", compute_type="int8")
        return self._whisper

    def speak(self, text: str) -> None:
        if not self.enabled or not text.strip():
            return
        def work() -> None:
            self.root.after(0, self.state_cb, "speaking")
            try:
                with self._lock:
                    voice = self._load_piper()
                with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as fh:
                    path = fh.name
                try:
                    with wave.open(path, "wb") as wav:
                        voice.synthesize_wav(text, wav)
                    import soundfile as sf
                    import sounddevice as sd
                    data, rate = sf.read(path, dtype="float32")
                    sd.play(data, rate)
                    sd.wait()
                finally:
                    try:
                        Path(path).unlink(missing_ok=True)
                    except OSError:
                        pass
            except Exception as exc:
                self.root.after(0, self.state_cb, f"voice error: {exc}")
            finally:
                self.root.after(0, self.state_cb, "idle")
        threading.Thread(target=work, daemon=True).start()

    def listen(self, callback, error_cb, seconds: float = 6.0) -> None:
        def work() -> None:
            self.root.after(0, self.state_cb, "listening")
            try:
                import sounddevice as sd
                import numpy as np
                rate = 16000
                audio = sd.rec(int(seconds * rate), samplerate=rate, channels=1,
                               dtype="float32")
                sd.wait()
                arr = np.asarray(audio, dtype="float32").reshape(-1)
                if arr.size == 0 or float(np.max(np.abs(arr))) < 0.005:
                    raise RuntimeError("no microphone signal (check input device)")
                with self._lock:
                    model = self._load_whisper()
                segments, _info = model.transcribe(arr, language="en", vad_filter=True)
                text = " ".join(s.text.strip() for s in segments).strip()
                if not text:
                    raise RuntimeError("nothing transcribed")
                self.root.after(0, callback, text)
            except Exception as exc:
                self.root.after(0, error_cb, f"{type(exc).__name__}: {exc}")
            finally:
                self.root.after(0, self.state_cb, "idle")
        threading.Thread(target=work, daemon=True).start()


class JarvisApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("J.A.R.V.I.S.")
        self.root.geometry("1240x800")
        self.root.minsize(980, 620)
        self.root.configure(bg=BG)
        self._streaming = False
        self._listening = False
        self._approvals: list[tk.Widget] = []
        self._nodes: list[dict] = []
        self._apps: list[tuple[str, str, bool]] = []
        self._build_style()
        self._build_ui()
        self.voice = VoiceIO(self.root, self._voice_state)
        self.refresh_health()
        self.refresh_nodes()
        self.poll_approvals()

    # ------------------------------------------------------------------ style
    def _build_style(self) -> None:
        s = ttk.Style(self.root)
        try:
            s.theme_use("clam")
        except tk.TclError:
            pass
        s.configure("TFrame", background=BG)
        s.configure("Panel.TFrame", background=PANEL)
        s.configure("TLabel", background=BG, foreground=FG)
        s.configure("Panel.TLabel", background=PANEL, foreground=FG)
        s.configure("Muted.TLabel", background=PANEL, foreground=MUTED)
        s.configure("Head.TLabel", background=BG, foreground=ACCENT,
                    font=("Segoe UI", 19, "bold"))
        s.configure("Sub.TLabel", background=BG, foreground=MUTED,
                    font=("Segoe UI", 9))
        s.configure("TButton", background=PANEL2, foreground=FG, borderwidth=0, padding=7)
        s.map("TButton", background=[("active", "#1b2b3a")])
        s.configure("Accent.TButton", background="#0f5f7a", foreground="#eaffff", padding=8)
        s.map("Accent.TButton", background=[("active", "#147b9d")])
        s.configure("TNotebook", background=BG, borderwidth=0)
        s.configure("TNotebook.Tab", background=PANEL2, foreground=MUTED, padding=(14, 7))
        s.map("TNotebook.Tab", background=[("selected", PANEL)], foreground=[("selected", ACCENT)])
        s.configure("Treeview", background=PANEL, fieldbackground=PANEL,
                    foreground=FG, rowheight=24, borderwidth=0)
        s.configure("Treeview.Heading", background=PANEL2, foreground=MUTED)
        s.map("Treeview", background=[("selected", "#15445a")])

    # --------------------------------------------------------------------- UI
    def _build_ui(self) -> None:
        header = ttk.Frame(self.root)
        header.pack(fill="x", padx=18, pady=(12, 4))
        self.orb = tk.Canvas(header, width=46, height=46, bg=BG, highlightthickness=0)
        self.orb.pack(side="left", padx=(0, 10))
        self.orb_ring = self.orb.create_oval(4, 4, 42, 42, outline="#1d3b4d", width=2)
        self.orb_core = self.orb.create_oval(11, 11, 35, 35, fill="#1b6b85", outline="")
        ttk.Label(header, text="J.A.R.V.I.S.", style="Head.TLabel").pack(side="left")
        ttk.Label(header, text="APOS CORE  /  NATIVE CLIENT", style="Sub.TLabel").pack(side="left", padx=(14, 0))
        self.status = ttk.Label(header, text="connecting...", style="Sub.TLabel")
        self.status.pack(side="right")
        self.voice_btn = ttk.Button(header, text="Voice: on", command=self.toggle_voice)
        self.voice_btn.pack(side="right", padx=(0, 10))

        self.nb = ttk.Notebook(self.root)
        self.nb.pack(fill="both", expand=True, padx=18, pady=8)
        self.nb.bind("<<NotebookTabChanged>>", self._tab_changed)

        self._build_command()
        self._build_hosts()
        self._build_tasks()
        self._build_approvals()
        self._build_systems()
        self._build_settings()

        self.bottom_status = ttk.Label(self.root, text="", style="Sub.TLabel")
        self.bottom_status.pack(fill="x", padx=18, pady=(0, 8))

    def _tab(self, title: str) -> ttk.Frame:
        frame = ttk.Frame(self.nb, style="Panel.TFrame")
        self.nb.add(frame, text=title)
        return frame

    # -- Command ------------------------------------------------------------
    def _build_command(self) -> None:
        f = self._tab("Command")
        wrap = ttk.Frame(f, style="Panel.TFrame")
        wrap.pack(fill="both", expand=True, padx=10, pady=10)
        self.transcript = tk.Text(wrap, bg=PANEL, fg=FG, insertbackground=FG, wrap="word",
                                  relief="flat", padx=14, pady=12, font=("Segoe UI", 11))
        scroll = ttk.Scrollbar(wrap, orient="vertical", command=self.transcript.yview)
        self.transcript.configure(yscrollcommand=scroll.set)
        self.transcript.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.transcript.configure(state="disabled")
        self.transcript.tag_configure("owner", foreground=ACCENT, font=("Segoe UI", 11, "bold"))
        self.transcript.tag_configure("jarvis", foreground=FG)
        self.transcript.tag_configure("system", foreground=MUTED, font=("Segoe UI", 10, "italic"))
        self.transcript.tag_configure("tool", foreground=WARN)
        self.transcript.tag_configure("error", foreground=ERR)
        self._say("system", "JARVIS native client ready. APOS is the brain; PC and laptop are agents.")

        bar = ttk.Frame(f, style="Panel.TFrame")
        bar.pack(fill="x", padx=10, pady=(0, 10))
        self.input = tk.Entry(bar, bg=PANEL2, fg=FG, insertbackground=ACCENT,
                              relief="flat", font=("Segoe UI", 11))
        self.input.pack(side="left", fill="x", expand=True, ipady=8)
        self.input.bind("<Return>", lambda _e: self.send())
        self.send_btn = ttk.Button(bar, text="Send", style="Accent.TButton", command=self.send)
        self.send_btn.pack(side="left", padx=(8, 0))
        self.mic_btn = ttk.Button(bar, text="🎙 Listen", command=self.listen)
        self.mic_btn.pack(side="left", padx=(8, 0))
        ttk.Button(bar, text="Clear", command=self._clear_chat).pack(side="left", padx=(8, 0))
        self.input.focus_set()

    def _clear_chat(self) -> None:
        self.transcript.configure(state="normal")
        self.transcript.delete("1.0", "end")
        self.transcript.configure(state="disabled")

    def _voice_state(self, state: str) -> None:
        colors = {"idle": "#1b6b85", "listening": WARN, "speaking": "#a77bff",
                  "thinking": ACCENT, "error": ERR}
        color = colors.get(state, MUTED)
        try:
            self.orb.itemconfig(self.orb_core, fill=color)
            self.orb.itemconfig(self.orb_ring, outline=color)
            self.orb_ring = self.orb.create_oval(4, 4, 42, 42, outline=color, width=2)
        except tk.TclError:
            pass
        if state != "idle":
            self.bottom_status.configure(text=f"voice: {state}")

    def toggle_voice(self) -> None:
        self.voice.enabled = not self.voice.enabled
        self.voice_btn.configure(text=f"Voice: {'on' if self.voice.enabled else 'off'}")
        self.bottom_status.configure(text=f"speech output {'enabled' if self.voice.enabled else 'muted'}")

    def listen(self) -> None:
        if self._listening:
            return
        self._listening = True
        self.mic_btn.configure(state="disabled")
        self._voice_state("listening")
        self.voice.listen(self._heard, self._listen_error)

    def _heard(self, text: str) -> None:
        self._listening = False
        self.mic_btn.configure(state="normal")
        self._voice_state("idle")
        if text:
            self.send_text(text)

    def _listen_error(self, error: str) -> None:
        self._listening = False
        self.mic_btn.configure(state="normal")
        self._voice_state("error")
        self._say("error", f"voice input: {error}")

    def _say(self, kind: str, text: str) -> None:
        self.transcript.configure(state="normal")
        self.transcript.insert("end", f"\n{kind.upper()}  {text}\n", (kind,))
        self.transcript.see("end")
        self.transcript.configure(state="disabled")

    def _token(self, text: str) -> None:
        self.transcript.configure(state="normal")
        self.transcript.insert("end", text, ("jarvis",))
        self.transcript.see("end")
        self.transcript.configure(state="disabled")

    # -- Hosts --------------------------------------------------------------
    def _build_hosts(self) -> None:
        f = self._tab("Hosts")
        top = ttk.Frame(f, style="Panel.TFrame")
        top.pack(fill="x", padx=10, pady=(10, 4))
        ttk.Label(top, text="CLUSTER", style="Muted.TLabel").pack(side="left")
        ttk.Button(top, text="Refresh", command=self.refresh_nodes).pack(side="right")
        self.host_tree = ttk.Treeview(f, columns=("host", "status", "url"), show="headings", height=6)
        for c, w in (("host", 110), ("status", 100), ("url", 330)):
            self.host_tree.heading(c, text=c.upper())
            self.host_tree.column(c, width=w, anchor="w")
        self.host_tree.pack(fill="x", padx=10)
        self.host_tree.bind("<<TreeviewSelect>>", lambda _e: self.load_host_apps())

        row = ttk.Frame(f, style="Panel.TFrame")
        row.pack(fill="x", padx=10, pady=8)
        ttk.Label(row, text="APPS ON SELECTED HOST", style="Muted.TLabel").pack(side="left")
        ttk.Button(row, text="Open via JARVIS", style="Accent.TButton",
                   command=self.open_selected_app).pack(side="right")
        self.apps_tree = ttk.Treeview(f, columns=("app", "target", "installed"),
                                      show="headings", height=13)
        for c, w in (("app", 160), ("target", 480), ("installed", 90)):
            self.apps_tree.heading(c, text=c.upper())
            self.apps_tree.column(c, width=w, anchor="w")
        self.apps_tree.pack(fill="both", expand=True, padx=10, pady=(0, 10))

    def refresh_nodes(self) -> None:
        def work() -> None:
            try:
                nodes = get_json("/api/nodes").get("nodes") or []
                self.root.after(0, self._nodes_loaded, nodes)
            except Exception:
                pass
            self.root.after(15000, self.refresh_nodes)
        threading.Thread(target=work, daemon=True).start()

    def _nodes_loaded(self, nodes: list[dict]) -> None:
        self._nodes = nodes
        current = self.host_tree.selection()
        keep = current[0] if current else None
        self.host_tree.delete(*self.host_tree.get_children())
        for n in nodes:
            self.host_tree.insert("", "end", iid=str(n.get("name") or n.get("id")),
                                  values=(n.get("name", "?"), n.get("status", "?"),
                                          n.get("base_url", "")))
        if keep and self.host_tree.exists(keep):
            self.host_tree.selection_set(keep)
        elif nodes:
            first = str(nodes[0].get("name") or nodes[0].get("id"))
            self.host_tree.selection_set(first)
            self.load_host_apps()
        self.bottom_status.configure(text=f"{len(nodes)} host(s) · {time.strftime('%H:%M:%S')}")

    def load_host_apps(self) -> None:
        sel = self.host_tree.selection()
        if not sel:
            return
        host = sel[0]

        def work() -> None:
            try:
                d = get_json(f"/api/hosts/{host}/apps")
                node_payload = d.get("data") or {}
                apps = ((node_payload.get("data") or {}).get("apps") or {})
                installed = ((node_payload.get("data") or {}).get("installed") or {})
                rows = [(name, str(target), bool(installed.get(name)))
                        for name, target in apps.items()]
                self.root.after(0, self._apps_loaded, rows)
            except Exception as exc:
                self.root.after(0, self._apps_loaded, [], str(exc))
        threading.Thread(target=work, daemon=True).start()

    def _apps_loaded(self, rows: list[tuple[str, str, bool]], error: str = "") -> None:
        self._apps = rows
        self.apps_tree.delete(*self.apps_tree.get_children())
        for name, target, installed in rows:
            self.apps_tree.insert("", "end", iid=name,
                                  values=(name, target, "yes" if installed else "no"))
        if error:
            self.root.after(0, lambda: self._say("error", f"apps: {error}"))

    def open_selected_app(self) -> None:
        host = self.host_tree.selection()
        app = self.apps_tree.selection()
        if not host or not app:
            self._say("system", "Select a host and an app first.")
            self.nb.select(0)
            return
        self.nb.select(0)
        self.send_text(f"Open {app[0]} on {host[0]}, using host.apps.launch.")

    # -- Tasks --------------------------------------------------------------
    def _build_tasks(self) -> None:
        f = self._tab("Tasks")
        top = ttk.Frame(f, style="Panel.TFrame")
        top.pack(fill="x", padx=10, pady=(10, 4))
        ttk.Label(top, text="RECENT TASKS", style="Muted.TLabel").pack(side="left")
        ttk.Button(top, text="Refresh", command=self.refresh_tasks).pack(side="right")
        ttk.Button(top, text="Cancel task", command=self.cancel_task).pack(side="right", padx=4)
        self.task_tree = ttk.Treeview(f, columns=("id", "time", "status", "request"),
                                      show="headings", height=9)
        for c, w in (("id", 110), ("time", 160), ("status", 110), ("request", 480)):
            self.task_tree.heading(c, text=c.upper())
            self.task_tree.column(c, width=w, anchor="w")
        self.task_tree.pack(fill="x", padx=10)
        self.task_tree.bind("<<TreeviewSelect>>", lambda _e: self.load_task_detail())
        self.task_detail = tk.Text(f, bg=PANEL, fg=FG, relief="flat", height=14,
                                   font=("Consolas", 9), padx=12, pady=10)
        self.task_detail.pack(fill="both", expand=True, padx=10, pady=10)
        self.task_detail.configure(state="disabled")
        self.refresh_tasks()

    def refresh_tasks(self) -> None:
        def work() -> None:
            try:
                tasks = get_json("/api/tasks?limit=50").get("tasks") or []
                self.root.after(0, self._tasks_loaded, tasks)
            except Exception:
                pass
        threading.Thread(target=work, daemon=True).start()

    def _tasks_loaded(self, tasks: list[dict]) -> None:
        self.task_tree.delete(*self.task_tree.get_children())
        for t in tasks:
            self.task_tree.insert("", "end", iid=str(t.get("id")),
                                  values=(t.get("id", "")[:24], t.get("created_at", ""),
                                          t.get("status", ""), str(t.get("request", ""))[:120]))

    def _task_id(self) -> str | None:
        sel = self.task_tree.selection()
        return sel[0] if sel else None

    def load_task_detail(self) -> None:
        tid = self._task_id()
        if not tid:
            return
        def work() -> None:
            try:
                d = get_json(f"/api/tasks/{tid}")
                self.root.after(0, self._detail, json.dumps(d, indent=2, default=str)[:12000])
            except Exception as exc:
                self.root.after(0, self._detail, f"error: {exc}")
        threading.Thread(target=work, daemon=True).start()

    def _detail(self, text: str) -> None:
        self.task_detail.configure(state="normal")
        self.task_detail.delete("1.0", "end")
        self.task_detail.insert("end", text)
        self.task_detail.configure(state="disabled")

    def cancel_task(self) -> None:
        tid = self._task_id()
        if not tid:
            return
        def work() -> None:
            try:
                r = post_json(f"/api/tasks/{tid}/cancel", {})
                self.root.after(0, lambda: self._say("system", f"cancel: {r}"))
                self.root.after(0, self.refresh_tasks)
            except Exception as exc:
                self.root.after(0, lambda: self._say("error", f"cancel: {exc}"))
        threading.Thread(target=work, daemon=True).start()

    # -- Approvals ----------------------------------------------------------
    def _build_approvals(self) -> None:
        f = self._tab("Approvals")
        ttk.Label(f, text="PENDING ACTIONS", style="Muted.TLabel").pack(anchor="w", padx=14, pady=(12, 4))
        self.approvals_frame = ttk.Frame(f, style="Panel.TFrame")
        self.approvals_frame.pack(fill="both", expand=True, padx=10, pady=8)

    def poll_approvals(self) -> None:
        def work() -> None:
            try:
                pending = get_json("/api/approvals").get("pending") or []
                self.root.after(0, self._approvals_loaded, pending)
            except Exception:
                pass
            self.root.after(2000, self.poll_approvals)
        threading.Thread(target=work, daemon=True).start()

    def _approvals_loaded(self, pending: list[dict]) -> None:
        for w in self._approvals:
            w.destroy()
        self._approvals.clear()
        if not pending:
            label = ttk.Label(self.approvals_frame, text="No pending approvals.",
                              style="Panel.TLabel")
            label.pack(anchor="w", padx=8, pady=8)
            self._approvals.append(label)
            return
        for a in pending[:6]:
            card = ttk.Frame(self.approvals_frame, style="Panel.TFrame")
            card.pack(fill="x", pady=4, padx=4)
            ttk.Label(card, text=f"{a.get('tool','?')}  [{a.get('side_effect','?')}]",
                      style="Panel.TLabel", font=("Segoe UI", 11, "bold")).pack(anchor="w", padx=4)
            ttk.Label(card, text=str(a.get("reason", ""))[:180], style="Muted.TLabel",
                      wraplength=800, justify="left").pack(anchor="w", padx=4)
            args = a.get("args") or {}
            ttk.Label(card, text=json.dumps(args, default=str)[:300], style="Muted.TLabel",
                      wraplength=900, justify="left").pack(anchor="w", padx=4)
            row = ttk.Frame(card, style="Panel.TFrame")
            row.pack(fill="x", pady=4)
            for text, decision, ttl, session in (("Allow once", "allow", 0, False),
                                                  ("Allow 1h", "allow", 3600, False),
                                                  ("Allow session", "allow", 0, True),
                                                  ("Deny", "deny", 0, False)):
                ttk.Button(row, text=text,
                           command=lambda i=a["id"], d=decision, t=ttl, s=session:
                           self._decide(i, d, t, s)).pack(side="left", padx=3)
            self._approvals.append(card)

    def _decide(self, approval_id: str, decision: str, remember_s: int, session: bool) -> None:
        body: dict = {"decision": decision}
        if remember_s:
            body["remember_s"] = remember_s
        if session:
            body["remember_session"] = True
        def work() -> None:
            try:
                post_json(f"/api/approvals/{approval_id}", body)
                self.root.after(0, lambda: self._say("system", f"approval {decision}"))
                self.root.after(0, self.poll_approvals)
            except Exception as exc:
                self.root.after(0, lambda: self._say("error", f"approval: {exc}"))
        threading.Thread(target=work, daemon=True).start()

    # -- Systems ------------------------------------------------------------
    def _build_systems(self) -> None:
        f = self._tab("Systems")
        ttk.Button(f, text="Refresh systems", command=self.refresh_systems).pack(anchor="e", padx=12, pady=8)
        self.system_text = tk.Text(f, bg=PANEL, fg=FG, relief="flat",
                                   font=("Consolas", 9), padx=12, pady=10)
        self.system_text.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        self.system_text.configure(state="disabled")
        self.refresh_systems()

    def refresh_systems(self) -> None:
        def work() -> None:
            parts = {}
            for name, path in (("health", "/api/health"), ("nodes", "/api/nodes"),
                               ("runtime", "/api/runtime"), ("sandbox", "/api/sandbox"),
                               ("audit", "/api/audit/verify")):
                try:
                    parts[name] = get_json(path)
                except Exception as exc:
                    parts[name] = {"error": str(exc)}
            text = "\n".join(f"=== {k.upper()} ===\n{json.dumps(v, indent=2, default=str)}\n"
                             for k, v in parts.items())
            self.root.after(0, self._systems_text, text[:20000])
        threading.Thread(target=work, daemon=True).start()

    def _systems_text(self, text: str) -> None:
        self.system_text.configure(state="normal")
        self.system_text.delete("1.0", "end")
        self.system_text.insert("end", text)
        self.system_text.configure(state="disabled")

    # -- Settings -----------------------------------------------------------
    def _build_settings(self) -> None:
        f = self._tab("Settings")
        ttk.Button(f, text="Refresh settings", command=self.refresh_settings).pack(anchor="e", padx=12, pady=8)
        self.settings_text = tk.Text(f, bg=PANEL, fg=FG, relief="flat",
                                     font=("Consolas", 9), padx=12, pady=10)
        self.settings_text.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        self.settings_text.configure(state="disabled")
        self.refresh_settings()

    def refresh_settings(self) -> None:
        def work() -> None:
            try:
                d = get_json("/api/settings")
                text = json.dumps(d, indent=2, default=str)[:20000]
            except Exception as exc:
                text = f"error: {exc}"
            self.root.after(0, self._settings_text, text)
        threading.Thread(target=work, daemon=True).start()

    def _settings_text(self, text: str) -> None:
        self.settings_text.configure(state="normal")
        self.settings_text.delete("1.0", "end")
        self.settings_text.insert("end", text)
        self.settings_text.configure(state="disabled")

    # -- Health / chat ------------------------------------------------------
    def refresh_health(self) -> None:
        def work() -> None:
            try:
                h = get_json("/api/health")
                self.root.after(0, self._health, h)
            except Exception as exc:
                self.root.after(0, lambda: self.status.configure(text="CORE UNREACHABLE",
                                                                 foreground=ERR))
            self.root.after(12000, self.refresh_health)
        threading.Thread(target=work, daemon=True).start()

    def _health(self, h: dict) -> None:
        models = (h.get("llm") or {}).get("models") or []
        self.status.configure(text=f"LIVE · {h.get('core','?')} · {h.get('tools','?')} tools · "
                                   f"{', '.join(models) or 'no model'}", foreground=OK)
        self.bottom_status.configure(text=f"mode {h.get('mode','?')} · DEFCON {h.get('defcon','?')} · "
                                          f"runtime {(h.get('runtime') or {}).get('available','?')}")

    def send(self) -> None:
        text = self.input.get().strip()
        if text:
            self.input.delete(0, "end")
            self.send_text(text)

    def send_text(self, text: str) -> None:
        if self._streaming:
            return
        self._streaming = True
        self.send_btn.configure(state="disabled")
        self._voice_state("thinking")
        self._say("owner", text)
        self._say("jarvis", "")
        threading.Thread(target=self._chat_worker, args=(text,), daemon=True).start()

    def _chat_worker(self, message: str) -> None:
        reply_parts: list[str] = []
        req = Request(BASE + "/api/chat", method="POST",
                      data=json.dumps({"message": message, "session_id": SESSION,
                                       "source": "text"}).encode(),
                      headers={"Content-Type": "application/json",
                               "Accept": "text/event-stream"})
        try:
            with urlopen(req, timeout=300) as resp:
                for raw in resp:
                    line = raw.decode("utf-8", "replace").strip()
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        ev = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    kind = ev.get("kind")
                    if kind == "token":
                        piece = str(ev.get("text", ""))
                        reply_parts.append(piece)
                        self.root.after(0, self._token, piece)
                    elif kind == "tool_call":
                        self.root.after(0, self._say, "tool", str(ev.get("tool", "")))
                    elif kind == "error":
                        self.root.after(0, self._say, "error", str(ev.get("text", "")))
                    elif kind == "final":
                        self.root.after(0, self._token, "\n")
            reply = "".join(reply_parts).strip()
            if reply and self.voice.enabled:
                self.voice.speak(reply)
        except HTTPError as exc:
            self.root.after(0, self._say, "error", f"HTTP {exc.code}")
        except URLError as exc:
            self.root.after(0, self._say, "error", f"connection: {exc.reason}")
        except Exception as exc:
            self.root.after(0, self._say, "error", f"{type(exc).__name__}: {exc}")
        finally:
            self.root.after(0, self._done)

    def _done(self) -> None:
        self._streaming = False
        self.send_btn.configure(state="normal")
        self._voice_state("idle")
        self.input.focus_set()

    # -- tab selection ------------------------------------------------------
    def _tab_changed(self, _event: object) -> None:
        tab = self.nb.tab(self.nb.select(), "text")
        if tab == "Tasks":
            self.refresh_tasks()
        elif tab == "Systems":
            self.refresh_systems()
        elif tab == "Settings":
            self.refresh_settings()
        elif tab == "Hosts":
            self.refresh_nodes()


def main() -> int:
    root = tk.Tk()
    JarvisApp(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
