"""EVOLVE — desktop GUI (tkinter, stdlib-only).

Three tabs — symbolic regression, numeric optimisation, and self-evolution —
each with inputs, a Run button, and a results pane. Runs execute in a background
thread so the window stays responsive; results are posted back on the tk thread.

Launch with ``python -m jarvis.evolve gui`` (or ``scripts\\evolve-gui.cmd``).
"""

from __future__ import annotations

import threading
import time
import tkinter as tk
from tkinter import ttk
from typing import Any, Callable

from .engine import EvolutionEngine, RunConfig
from .genome import expression_spec, expression_str, numeric_spec
from .meta import self_tune
from .objectives import rastrigin, sphere, symbolic_regression

__all__ = ["gui_main"]

_BENCHMARKS = {"sphere": sphere, "rastrigin": rastrigin}


def _nums(text: str) -> list[float]:
    return [float(x) for x in text.replace(",", " ").split() if x.strip()]


def gui_main() -> None:
    root = tk.Tk()
    root.title("EVOLVE — Evolutionary Intelligence Lab")
    root.geometry("780x540")
    root.minsize(680, 460)

    style = ttk.Style(root)
    style.theme_use("clam")

    status = tk.StringVar(value="Ready. Choose a tab and press Run.")
    notebook = ttk.Notebook(root)
    notebook.pack(fill="both", expand=True, padx=8, pady=8)

    # ------------------------------------------------------------- helpers ---
    def run_async(work: Callable[[], str], *, set_busy: Callable[[], None] = None,
                  set_idle: Callable[[], None] = None,
                  show: Callable[[str], None] = None,
                  status_text: str = "Running…") -> None:
        """Run ``work`` on a worker thread and show its string result."""
        if set_busy:
            set_busy()
        status.set(status_text)

        def worker() -> None:
            try:
                text = work()
            except Exception as exc:  # pragma: no cover - UI must not crash
                text = f"error: {type(exc).__name__}: {exc}"
            root.after(0, lambda: _finish(text))

        def _finish(text: str) -> None:
            if set_idle:
                set_idle()
            if show:
                show(text)
            status.set("Done.")

        threading.Thread(target=worker, daemon=True).start()

    # ------------------------------------------------------- symbolic tab ----
    sym = ttk.Frame(notebook, padding=10)
    notebook.add(sym, text=" Symbolic regression ")

    xs_var = tk.StringVar(value="-3,-2,-1,0,1,2,3")
    ys_var = tk.StringVar(value="8,3,0,-1,0,3,8")  # y = x^2 + 2x - 1
    gens_var = tk.StringVar(value="100")
    pop_var = tk.StringVar(value="150")

    def _field(parent: Any, row: int, label: str, var: tk.StringVar) -> None:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=4)
        ttk.Entry(parent, textvariable=var, width=48).grid(row=row, column=1, sticky="we", pady=4)

    _field(sym, 0, "X points (comma-separated)", xs_var)
    _field(sym, 1, "Y points (same length)", ys_var)
    _field(sym, 2, "Generations", gens_var)
    _field(sym, 3, "Population", pop_var)
    sym.columnconfigure(1, weight=1)

    sym_btn = ttk.Button(sym, text="▶  Evolve the formula")

    def run_symbolic() -> str:
        try:
            xs = _nums(xs_var.get())
            ys = _nums(ys_var.get())
            generations = int(gens_var.get())
            population = int(pop_var.get())
        except ValueError:
            return "error: numbers must be numeric (and generations/population integers)"
        if len(xs) != len(ys) or not xs:
            return "error: X and Y must be matching non-empty lists"
        fitness = symbolic_regression(xs, ys)
        cfg = RunConfig(population_size=population, generations=generations,
                        mutation_rate=0.35, crossover_rate=0.9, elitism=2,
                        tournament_size=3, seed=0, max_seconds=300.0,
                        stagnation_generations=30)
        t0 = time.perf_counter()
        res = EvolutionEngine(expression_spec, fitness, cfg).run()
        return (f"Recovered expression:\n\n    f(x) = {expression_str(res.best.genome)}\n\n"
                f"Mean-squared error:  {res.best.fitness:.6f}\n"
                f"Generations run:     {res.generations_run}\n"
                f"Wall time:           {time.perf_counter() - t0:.2f}s")

    sym_btn.grid(row=4, column=0, columnspan=2, pady=8)

    sym_box = tk.Text(sym, height=12, wrap="word", font=("Consolas", 10))
    sym_box.grid(row=5, column=0, columnspan=2, sticky="nsew")
    sym.rowconfigure(5, weight=1)

    def _sym_show(text: str) -> None:
        sym_box.delete("1.0", "end")
        sym_box.insert("1.0", text)

    sym_btn.config(command=lambda: run_async(
        run_symbolic,
        set_busy=lambda: sym_btn.config(state="disabled"),
        set_idle=lambda: sym_btn.config(state="normal"),
        show=_sym_show,
        status_text="Evolving formula…"))

    # ------------------------------------------------------- minimize tab ----
    minf = ttk.Frame(notebook, padding=10)
    notebook.add(minf, text=" Minimize ")

    fn_var = tk.StringVar(value="sphere")
    dim_var = tk.StringVar(value="4")
    mg_var = tk.StringVar(value="60")
    min_out = tk.StringVar(value="(results appear here)")

    ttk.Label(minf, text="Function").grid(row=0, column=0, sticky="w", pady=4)
    ttk.Combobox(minf, textvariable=fn_var, values=("sphere", "rastrigin"),
                 state="readonly", width=20).grid(row=0, column=1, sticky="w")
    _field(minf, 1, "Dimensions (1..50)", dim_var)
    _field(minf, 2, "Generations", mg_var)

    min_btn = ttk.Button(minf, text="▶  Minimize")

    def run_minimize() -> str:
        try:
            fn = _BENCHMARKS[fn_var.get()]
            dims = int(dim_var.get())
            generations = int(mg_var.get())
        except (KeyError, ValueError):
            return "error: check function/dimensions/generations"
        spec = numeric_spec([(-5.12, 5.12)] * dims)
        cfg = RunConfig(population_size=80, generations=generations,
                        mutation_rate=0.3, crossover_rate=0.8, elitism=2,
                        tournament_size=3, seed=0, max_seconds=300.0,
                        stagnation_generations=20)
        t0 = time.perf_counter()
        res = EvolutionEngine(spec, fn, cfg).run()
        vec = [round(v, 5) for v in res.best.genome]
        return (f"Function: {fn_var.get()}  (global minimum = 0)\n\n"
                f"Best vector: {vec}\n\n"
                f"Best cost:   {res.best.fitness:.6f}\n"
                f"Wall time:   {time.perf_counter() - t0:.2f}s")

    min_btn.config(command=lambda: run_async(
        run_minimize,
        set_busy=lambda: min_btn.config(state="disabled"),
        set_idle=lambda: min_btn.config(state="normal"),
        show=lambda t: min_out.set(t),
        status_text="Minimizing…"))
    min_btn.grid(row=3, column=0, columnspan=2, pady=8)
    ttk.Label(minf, textvariable=min_out, wraplength=640, justify="left",
              font=("Consolas", 10)).grid(row=4, column=0, columnspan=2, sticky="nw")

    # ----------------------------------------------------- self-evolve tab ---
    st = ttk.Frame(notebook, padding=10)
    notebook.add(st, text=" Self-evolve ")
    st_out = tk.StringVar(value="(results appear here)")
    st_task = tk.StringVar(value="sphere")
    st_meta = tk.StringVar(value="8")

    ttk.Label(st, text="Benchmark task").grid(row=0, column=0, sticky="w", pady=4)
    ttk.Combobox(st, textvariable=st_task, values=("sphere", "rastrigin"),
                 state="readonly", width=20).grid(row=0, column=1, sticky="w")
    _field(st, 1, "Meta generations", st_meta)

    st_btn = ttk.Button(st, text="▶  Evolve the engine itself")

    def run_selftune() -> str:
        try:
            fn = _BENCHMARKS[st_task.get()]
            meta_generations = int(st_meta.get())
        except (KeyError, ValueError):
            return "error: check task/meta generations"
        spec = numeric_spec([(-5.0, 5.0)] * 2)
        t0 = time.perf_counter()
        res = self_tune(spec, fn, meta_generations=meta_generations,
                        task_generations=15, task_population=24, eval_seeds=(0,),
                        max_seconds=300.0)
        return (f"Task: {st_task.get()}\n\n"
                f"Winning engine config:\n\n    {res.best.genome}\n\n"
                f"Meta cost:        {res.best.fitness:.6f}\n"
                f"Meta generations: {res.generations_run}\n"
                f"Wall time:        {time.perf_counter() - t0:.2f}s")

    st_btn.config(command=lambda: run_async(
        run_selftune,
        set_busy=lambda: st_btn.config(state="disabled"),
        set_idle=lambda: st_btn.config(state="normal"),
        show=lambda t: st_out.set(t),
        status_text="Self-evolving…"))
    st_btn.grid(row=2, column=0, columnspan=2, pady=8)
    ttk.Label(st, textvariable=st_out, wraplength=640, justify="left",
              font=("Consolas", 10)).grid(row=3, column=0, columnspan=2, sticky="nw")

    # --------------------------------------------------------- status bar ---
    ttk.Separator(root, orient="horizontal").pack(fill="x")
    ttk.Label(root, textvariable=status, padding=(8, 4)).pack(fill="x")

    root.mainloop()


if __name__ == "__main__":
    gui_main()
