"""EVOLVE — desktop GUI (tkinter, stdlib-only).

User-friendly lab: live fitness charts, progress readouts, five benchmark
functions, symbolic regression, self-evolution, and an **automatic** queue that
runs a whole experiment suite (Night-Protocol bounded) with a live log.

Launch with ``python -m jarvis.evolve gui`` (or ``scripts\\evolve-gui.cmd``).
"""

from __future__ import annotations

import math
import threading
import time
import tkinter as tk
from tkinter import ttk
from typing import Any, Callable

from .engine import EvolutionEngine, RunConfig
from .genome import expression_spec, expression_str, numeric_spec
from .meta import self_tune
from .objectives import (ackley, griewank, rastrigin, rosenbrock, sphere,
                         symbolic_regression)

__all__ = ["gui_main"]

BENCHMARKS = {
    "sphere": sphere,
    "rastrigin": rastrigin,
    "ackley": ackley,
    "rosenbrock": rosenbrock,
    "griewank": griewank,
}

BG = "#0d1420"
FG = "#cfe8ff"
CYAN = "#3ce0ff"
DIM = "#5a7a9a"
GOOD = "#57d9a3"


def _nums(text: str) -> list[float]:
    return [float(x) for x in text.replace(",", " ").split() if x.strip()]


# --------------------------------------------------------------------------- #
# Live fitness chart
# --------------------------------------------------------------------------- #
class FitnessChart(tk.Canvas):
    """A live best-fitness curve with automatic linear/log scaling."""

    def __init__(self, parent: Any, height: int = 150) -> None:
        super().__init__(parent, height=height, bg=BG, highlightthickness=0)
        self.series: list[tuple[int, float]] = []
        self.bind("<Configure>", lambda e: self.redraw())

    def reset(self) -> None:
        self.series = []
        self.redraw()

    def add(self, gen: int, best: float) -> None:
        self.series.append((gen, best))
        self.redraw()

    def redraw(self) -> None:
        self.delete("all")
        w = max(self.winfo_width(), 240)
        h = max(self.winfo_height(), 90)
        pts = self.series
        if len(pts) < 2:
            self.create_text(w // 2, h // 2, fill=DIM, font=("Consolas", 9),
                             text="fitness curve appears here")
            return
        finite = [b for _, b in pts if b != float("inf")]
        if not finite:
            return
        ymin, ymax = min(finite), max(finite)
        use_log = ymin > 0 and ymax / max(ymin, 1e-12) > 500
        max_gen = max(pts[-1][0], 1)

        def sx(g: int) -> float:
            return 8 + (g / max_gen) * (w - 16)

        def sy(b: float) -> float:
            if b == float("inf"):
                return h - 8
            if use_log:
                lo = math.log(max(ymin, 1e-12))
                hi = math.log(max(ymax, 1e-12))
                v = math.log(max(b, 1e-12))
                return 8 + (1 - (v - lo) / (hi - lo or 1)) * (h - 16)
            return 8 + (1 - (b - ymin) / (ymax - ymin or 1)) * (h - 16)

        coords: list[float] = []
        for g, b in pts:
            coords += [sx(g), sy(b)]
        self.create_line(*coords, fill=CYAN, width=2)
        self.create_oval(sx(pts[-1][0]) - 3, sy(pts[-1][1]) - 3,
                         sx(pts[-1][0]) + 3, sy(pts[-1][1]) + 3,
                         fill=GOOD, outline=GOOD)
        scale = "log" if use_log else "linear"
        self.create_text(w - 8, 10, anchor="ne", fill=DIM, font=("Consolas", 8),
                         text=f"best {finite[-1]:.4g} · {scale} scale")


class RunPane(ttk.Frame):
    """Inputs + Run button + live chart + result text, shared by the tabs."""

    def __init__(self, parent: Any, run_label: str) -> None:
        super().__init__(parent, padding=8)
        self.run_label = run_label
        self.queue: list[tuple[int, float]] = []
        self.result_text: str | None = None
        self.gen_var = tk.StringVar(value="")
        self.best_var = tk.StringVar(value="")
        self.chart = FitnessChart(self)
        self.chart.grid(row=10, column=0, columnspan=2, sticky="nsew", pady=(8, 4))
        self.rowconfigure(10, weight=1)
        self.columnconfigure(1, weight=1)

        self.gen_label = ttk.Label(self, textvariable=self.gen_var, foreground=DIM)
        self.gen_label.grid(row=11, column=0, sticky="w")
        self.best_label = ttk.Label(self, textvariable=self.best_var, foreground=GOOD,
                                    font=("Consolas", 10, "bold"))
        self.best_label.grid(row=11, column=1, sticky="e")

        self.out = tk.Text(self, height=8, wrap="word", bg=BG, fg=FG,
                           insertbackground=FG, relief="flat", font=("Consolas", 9))
        self.out.grid(row=12, column=0, columnspan=2, sticky="nsew")
        self.rowconfigure(12, weight=1)

    def field(self, row: int, label: str, var: tk.StringVar) -> None:
        ttk.Label(self, text=label).grid(row=row, column=0, sticky="w", pady=3)
        ttk.Entry(self, textvariable=var, width=40).grid(row=row, column=1,
                                                         sticky="we", pady=3)

    def add_button(self, run_fn: Callable[[], None], row: int) -> ttk.Button:
        btn = ttk.Button(self, text=self.run_label, command=run_fn)
        btn.grid(row=row, column=0, columnspan=2, pady=6)
        return btn

    def show_result(self, text: str) -> None:
        self.out.delete("1.0", "end")
        self.out.insert("1.0", text)

    # -- live polling (tk thread only) ------------------------------------
    def drain(self) -> bool:
        """Pull worker-thread progress into the widgets. True when finished."""
        if self.queue:
            pts, self.queue[:] = self.queue, []
            for g, b in pts:
                self.chart.add(g, b)
            self.gen_var.set(f"generation {pts[-1][0]}")
            self.best_var.set(f"best {pts[-1][1]:.6g}")
        if self.result_text is not None:
            text, self.result_text = self.result_text, None
            self.show_result(text)
            self.run_btn.config(state="normal")
            return True
        return False

    def poll(self) -> None:
        if not self.drain():
            self.after(150, self.poll)


def _launch(pane: RunPane, root: tk.Tk, status: tk.StringVar,
            build_work: Callable[[Callable[[int, dict[str, Any]], None]],
                                 Callable[[], str]]) -> None:
    """Run ``build_work(on_generation)()`` on a worker thread with live updates."""
    pane.chart.reset()
    pane.queue = []
    pane.result_text = None
    pane.gen_var.set("starting…")
    pane.best_var.set("")
    pane.run_btn.config(state="disabled")
    status.set("Running…")

    def on_generation(gen: int, stats: dict[str, Any]) -> None:
        pane.queue.append((gen, stats["best"]))

    def worker() -> None:
        try:
            text = build_work(on_generation)()
        except Exception as exc:  # pragma: no cover
            text = f"error: {type(exc).__name__}: {exc}"
        pane.result_text = text

    threading.Thread(target=worker, daemon=True).start()
    root.after(150, pane.poll)


def gui_main() -> None:
    root = tk.Tk()
    root.title("EVOLVE — Evolutionary Intelligence Lab")
    root.geometry("860x640")
    root.minsize(720, 560)

    style = ttk.Style(root)
    style.theme_use("clam")
    status = tk.StringVar(value="Ready. Pick a tab and press Run — or open Auto.")
    notebook = ttk.Notebook(root)
    notebook.pack(fill="both", expand=True, padx=8, pady=(8, 0))

    # ------------------------------------------------------- symbolic tab ----
    sym = RunPane(notebook, "▶  Evolve the formula")
    notebook.add(sym, text=" Symbolic regression ")
    xs_var = tk.StringVar(value="-3,-2,-1,0,1,2,3")
    ys_var = tk.StringVar(value="8,3,0,-1,0,3,8")  # y = x^2 + 2x - 1
    gens_var = tk.StringVar(value="100")
    pop_var = tk.StringVar(value="150")
    sym.field(0, "X points (comma-separated)", xs_var)
    sym.field(1, "Y points (same length)", ys_var)
    sym.field(2, "Generations", gens_var)
    sym.field(3, "Population", pop_var)

    def run_symbolic(cb):
        def work() -> str:
            xs = _nums(xs_var.get())
            ys = _nums(ys_var.get())
            if len(xs) != len(ys) or not xs:
                return "error: X and Y must be matching non-empty lists"
            fitness = symbolic_regression(xs, ys)
            cfg = RunConfig(population_size=int(pop_var.get()),
                            generations=int(gens_var.get()),
                            mutation_rate=0.35, crossover_rate=0.9, elitism=2,
                            tournament_size=3, seed=0, max_seconds=300.0,
                            stagnation_generations=30, on_generation=cb)
            t0 = time.perf_counter()
            res = EvolutionEngine(expression_spec, fitness, cfg).run()
            return (f"Recovered expression:\n\n    f(x) = {expression_str(res.best.genome)}\n\n"
                    f"Mean-squared error: {res.best.fitness:.6f}\n"
                    f"Generations run:    {res.generations_run}\n"
                    f"Wall time:          {time.perf_counter() - t0:.2f}s")
        return work

    sym.run_btn = sym.add_button(
        lambda: _launch(sym, root, status, run_symbolic), 4)

    # ------------------------------------------------------- minimize tab ---
    minf = RunPane(notebook, "▶  Minimize")
    notebook.add(minf, text=" Minimize ")
    fn_var = tk.StringVar(value="sphere")
    dim_var = tk.StringVar(value="4")
    mg_var = tk.StringVar(value="80")
    mp_var = tk.StringVar(value="80")
    ttk.Label(minf, text="Function").grid(row=0, column=0, sticky="w", pady=3)
    ttk.Combobox(minf, textvariable=fn_var, values=sorted(BENCHMARKS),
                 state="readonly", width=30).grid(row=0, column=1, sticky="w")
    minf.field(1, "Dimensions (1..50)", dim_var)
    minf.field(2, "Generations", mg_var)
    minf.field(3, "Population", mp_var)

    def run_minimize(cb):
        def work() -> str:
            fn = BENCHMARKS[fn_var.get()]
            dims = int(dim_var.get())
            spec = numeric_spec([(-5.12, 5.12)] * dims)
            cfg = RunConfig(population_size=int(mp_var.get()),
                            generations=int(mg_var.get()),
                            mutation_rate=0.3, crossover_rate=0.8, elitism=2,
                            tournament_size=3, seed=0, max_seconds=300.0,
                            stagnation_generations=20, on_generation=cb)
            t0 = time.perf_counter()
            res = EvolutionEngine(spec, fn, cfg).run()
            return (f"Function: {fn_var.get()}   (global minimum = 0)\n\n"
                    f"Best vector: {[round(v, 5) for v in res.best.genome]}\n\n"
                    f"Best cost:   {res.best.fitness:.6f}\n"
                    f"Wall time:   {time.perf_counter() - t0:.2f}s")
        return work

    minf.run_btn = minf.add_button(
        lambda: _launch(minf, root, status, run_minimize), 4)

    # ----------------------------------------------------- self-evolve tab ---
    st = RunPane(notebook, "▶  Evolve the engine itself")
    notebook.add(st, text=" Self-evolve ")
    st_task = tk.StringVar(value="sphere")
    st_meta = tk.StringVar(value="8")
    ttk.Label(st, text="Benchmark task").grid(row=0, column=0, sticky="w", pady=3)
    ttk.Combobox(st, textvariable=st_task, values=sorted(BENCHMARKS),
                 state="readonly", width=30).grid(row=0, column=1, sticky="w")
    st.field(1, "Meta generations", st_meta)

    def run_selftune(cb):
        def work() -> str:
            fn = BENCHMARKS[st_task.get()]
            spec = numeric_spec([(-5.0, 5.0)] * 2)
            t0 = time.perf_counter()
            res = self_tune(spec, fn, meta_generations=int(st_meta.get()),
                            task_generations=15, task_population=24,
                            eval_seeds=(0,), max_seconds=300.0)
            return (f"Task: {st_task.get()}\n\n"
                    f"Winning engine config:\n\n    {res.best.genome}\n\n"
                    f"Meta cost:        {res.best.fitness:.6f}\n"
                    f"Meta generations: {res.generations_run}\n"
                    f"Wall time:        {time.perf_counter() - t0:.2f}s")
        return work

    st.run_btn = st.add_button(
        lambda: _launch(st, root, status, run_selftune), 2)

    # ---------------------------------------------------- server tuning tab ---
    tune = RunPane(notebook, "▶  Tune it (measure reality)")
    notebook.add(tune, text=" Server tuning ")
    t_target = tk.StringVar(value="command")
    ttk.Label(tune, text="Target").grid(row=0, column=0, sticky="w", pady=3)
    ttk.Combobox(tune, textvariable=t_target, values=("command", "ollama"),
                 state="readonly", width=30).grid(row=0, column=1, sticky="w")
    t_cmd = tk.StringVar(value='python -c "import time; time.sleep(0.02 * {level})"')
    t_low = tk.StringVar(value="1")
    t_high = tk.StringVar(value="9")
    tune.field(1, "Command (with {level})", t_cmd)
    tune.field(2, "Parameter range (low)", t_low)
    tune.field(3, "Parameter range (high)", t_high)

    def run_tune(cb):
        def work() -> str:
            target = t_target.get()
            if target == "command":
                from .measure import command_benchmark
                from .settings import Field, SettingsSpace
                from .tuner import SystemTuner
                space = SettingsSpace("command", {
                    "level": Field("int", low=int(t_low.get()), high=int(t_high.get()))})
                fitness = command_benchmark(t_cmd.get(), runs=2)
                tuner = SystemTuner(space, fitness, name="command")
                res = tuner.tune(generations=15, population=20,
                                 on_generation=cb, max_seconds=600.0)
                return (f"Tuned command parameter:\n\n"
                        f"    best {list(res.best.genome)[0]} = {list(res.best.genome.values())[0]}\n\n"
                        f"Mean wall time: {res.best.fitness:.4f}s\n"
                        f"Generations:    {res.generations_run}")
            # ollama
            from .measure import ollama_latency
            from .tuner import SystemTuner, ollama_space
            fitness = ollama_latency("http://127.0.0.1:11434", "qwen2.5:3b-instruct",
                                     "Say OK.", runs=1, warmup=0)
            tuner = SystemTuner(ollama_space(), fitness, name="ollama")
            res = tuner.tune(generations=8, population=10,
                             on_generation=cb, max_seconds=900.0)
            return (f"Tuned Ollama request settings:\n\n"
                    f"    {res.best.genome}\n\n"
                    f"Measured latency: {res.best.fitness:.3f}s\n"
                    f"Generations:      {res.generations_run}")
        return work

    tune.run_btn = tune.add_button(
        lambda: _launch(tune, root, status, run_tune), 4)

    # ------------------------------------------------------------- auto tab ---
    auto = ttk.Frame(notebook, padding=8)
    notebook.add(auto, text=" ⚙ Auto ")
    auto.columnconfigure(0, weight=1)

    mins_var = tk.StringVar(value="8")
    ttk.Label(auto, text="Time budget (minutes)").grid(row=0, column=0, sticky="w", pady=3)
    ttk.Entry(auto, textvariable=mins_var, width=12).grid(row=0, column=1, sticky="w")
    auto_btn = ttk.Button(auto, text="▶  Run the full experiment queue")
    auto_btn.grid(row=1, column=0, columnspan=2, pady=6, sticky="w")

    auto_log = tk.Text(auto, height=18, wrap="word", bg=BG, fg=FG,
                       insertbackground=FG, relief="flat", font=("Consolas", 9))
    auto_log.grid(row=2, column=0, columnspan=2, sticky="nsew", pady=(4, 0))
    auto.rowconfigure(2, weight=1)

    def run_auto() -> None:
        from .auto import AutoRunner
        auto_btn.config(state="disabled")
        status.set("Automatic run in progress…")
        auto_log.delete("1.0", "end")
        auto_log.insert("end", "Starting automatic queue…\n")

        def log_event(ev: dict[str, Any]) -> None:
            kind = ev.get("kind")
            if kind == "experiment_started":
                line = f"\n▶ {ev['experiment']} …\n"
            elif kind == "generation":
                line = None  # too chatty; keep the log readable
            elif kind == "experiment_done":
                line = (f"  ✔ done — best {ev['best_fitness']:.6g} "
                        f"({ev['generations']} gens, {ev['seconds']}s)\n")
            elif kind == "queue_done":
                rows = ev["report"]["rows"]
                line = "\n=== AUTOMATIC RUN COMPLETE ===\n"
                for r in rows:
                    b = r["best_str"] or str(r["best_genome"])
                    line += f"  {r['experiment']:22s} {r['best_fitness']:.6g}   {b}\n"
                line += f"  wall time: {ev['report']['wall_seconds']}s\n"
            elif kind == "budget_exhausted":
                line = "\n(⏱ time budget reached — remaining experiments skipped)\n"
            else:
                line = None
            if line:
                root.after(0, lambda: (auto_log.insert("end", line),
                                       auto_log.see("end")))

        def worker() -> None:
            try:
                report = AutoRunner(minutes=float(mins_var.get()),
                                    on_progress=log_event).run()
            except Exception as exc:  # pragma: no cover
                report = {"error": f"{type(exc).__name__}: {exc}"}
            root.after(0, lambda: _finish_auto(report))

        def _finish_auto(report: dict[str, Any]) -> None:
            auto_btn.config(state="normal")
            if "error" in report:
                auto_log.insert("end", f"\nerror: {report['error']}\n")
            status.set("Automatic run complete.")

        threading.Thread(target=worker, daemon=True).start()

    auto_btn.config(command=run_auto)

    # --------------------------------------------------------- status bar ---
    ttk.Separator(root, orient="horizontal").pack(fill="x")
    ttk.Label(root, textvariable=status, padding=(8, 4)).pack(fill="x")

    root.mainloop()


if __name__ == "__main__":
    gui_main()
