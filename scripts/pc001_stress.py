#!/usr/bin/env python3
"""PC-001 stress harness — push RAM / CPU / GPU to their limits, bounded.

Modes: cpu | ram | gpu | all

* ``ram`` allocates in chunks and touches every page until the target (default
  85 % of available) or a MemoryError — ``--ram-percent 100`` pushes to the
  absolute limit while still leaving ``--reserve-mb`` free.
* ``cpu`` runs one busy worker per logical core.
* ``gpu`` opens a WebGL shader page in Edge kiosk mode (no CUDA needed).
* ``all`` runs GPU + CPU + RAM together.

Every run is time-bound (``--seconds``) and prints a final summary. A hang or
BSOD is the test result; the machine cannot report afterwards by definition.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import time
import multiprocessing as mp

MB = 1024 * 1024


def burn(seconds: float) -> None:
    end = time.time() + seconds
    x = 0.0
    while time.time() < end:
        for i in range(20000):
            x = (x + i * i) % 1_000_003
    return


def cpu_mode(seconds: float, workers: int) -> None:
    procs = [mp.Process(target=burn, args=(seconds,)) for _ in range(workers)]
    for p in procs:
        p.start()
    for p in procs:
        p.join()


def ram_mode(seconds: float, percent: float, reserve_mb: int) -> None:
    import psutil

    available = psutil.virtual_memory().available
    usable = max(0, available - reserve_mb * MB)
    target = int(usable * (percent / 100.0))
    blocks: list[bytearray] = []
    chunk = 64 * MB
    filled = 0
    t0 = time.time()
    try:
        while filled < target and time.time() - t0 < seconds:
            block = bytearray(chunk)
            for offset in range(0, chunk, 4096):  # touch every page
                block[offset] = 0x5A
            blocks.append(block)
            filled += chunk
            if len(blocks) % 16 == 0:
                print(f"ram: {filled // MB} MB allocated", flush=True)
    except MemoryError:
        print(f"ram: MemoryError at {filled // MB} MB (absolute limit)", flush=True)
    held = filled
    print(f"ram: holding {held // MB} MB for {seconds:.0f}s", flush=True)
    time.sleep(seconds)
    blocks.clear()


GPU_HTML = """<!doctype html><html><body style="margin:0;background:#000">
<canvas id="c" width="3840" height="2160" style="width:100vw;height:100vh"></canvas>
<script>
const gl = document.getElementById('c').getContext('webgl2') ||
           document.getElementById('c').getContext('webgl');
const vs = `attribute vec2 p; void main(){gl_Position=vec4(p,0,1);}`;
const fs = `precision highp float; uniform float t;
void main(){ vec2 uv = gl_FragCoord.xy/800.0;
  vec3 c = vec3(0.0); vec2 z = uv;
  for(int i=0;i<900;i++){ z = vec2(z.x*z.x-z.y*z.y, 2.0*z.x*z.y) + uv + vec2(sin(t), cos(t))*0.01;
    c += 0.002*vec3(float(i%3), float(i%5), float(i%7)); }
  gl_FragColor = vec4(c,1.0); }`;
function sh(type, src){const s=gl.createShader(type);gl.shaderSource(s,src);
  gl.compileShader(s);return s;}
const prog = gl.createProgram();
gl.attachShader(prog, sh(gl.VERTEX_SHADER, vs));
gl.attachShader(prog, sh(gl.FRAGMENT_SHADER, fs));
gl.linkProgram(prog); gl.useProgram(prog);
const buf = gl.createBuffer(); gl.bindBuffer(gl.ARRAY_BUFFER, buf);
gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1,-1,3,-1,-1,3]), gl.STATIC_DRAW);
gl.enableVertexAttribArray(0);
gl.vertexAttribPointer(0,2,gl.FLOAT,false,0,0);
const tLoc = gl.getUniformLocation(prog, 't');
let start = performance.now();
function frame(){ gl.uniform1f(tLoc, (performance.now()-start)/1000.0);
  gl.drawArrays(gl.TRIANGLES,0,3); requestAnimationFrame(frame); }
frame();
</script></body></html>"""


def gpu_mode(seconds: float) -> subprocess.Popen | None:
    path = os.path.join(tempfile.gettempdir(), "pc001-gpu-stress.html")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(GPU_HTML)
    edge = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
    chrome = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
    exe = edge if os.path.exists(edge) else chrome
    if not os.path.exists(exe):
        print("gpu: no Edge/Chrome found", flush=True)
        return None
    proc = subprocess.Popen([exe, f"--kiosk=file:///{path.replace(os.sep, '/')}",
                             "--new-window", "--disable-features=CalculateNativeWinOcclusion"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print(f"gpu: WebGL shader running in {os.path.basename(exe)} for {seconds:.0f}s",
          flush=True)
    return proc


def main() -> int:
    parser = argparse.ArgumentParser(description="PC-001 absolute-limit stress")
    parser.add_argument("mode", choices=("cpu", "ram", "gpu", "all"))
    parser.add_argument("--seconds", type=float, default=300.0)
    parser.add_argument("--ram-percent", type=float, default=85.0,
                        help="percent of available RAM to allocate (100 = limit)")
    parser.add_argument("--reserve-mb", type=int, default=512)
    parser.add_argument("--workers", type=int, default=os.cpu_count() or 4)
    args = parser.parse_args()
    t0 = time.time()
    print(f"stress mode={args.mode} seconds={args.seconds} "
          f"ram={args.ram_percent}% reserve={args.reserve_mb}MB "
          f"workers={args.workers}", flush=True)
    gpu_proc = None
    try:
        if args.mode in ("gpu", "all"):
            gpu_proc = gpu_mode(args.seconds)
        if args.mode == "gpu":
            time.sleep(args.seconds)
        if args.mode in ("cpu", "all"):
            if args.mode == "all":
                # RAM and CPU together; GPU already running
                import threading
                t = threading.Thread(target=ram_mode,
                                     args=(args.seconds, args.ram_percent,
                                           args.reserve_mb), daemon=True)
                t.start()
                cpu_mode(args.seconds, args.workers)
                t.join(timeout=args.seconds + 30)
            else:
                cpu_mode(args.seconds, args.workers)
        elif args.mode == "ram":
            ram_mode(args.seconds, args.ram_percent, args.reserve_mb)
        elif args.mode == "gpu":
            pass
    finally:
        if gpu_proc is not None:
            gpu_proc.terminate()
            try:
                gpu_proc.wait(timeout=10)
            except Exception:
                gpu_proc.kill()
    print(f"stress: finished cleanly in {time.time() - t0:.1f}s", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
