/* JARVIS orbital visualization — state-driven, calm amber core.
   Pure rendering: consumes state supplied by existing HUD, makes no network calls. */
const PALETTES = {
  idle: {h:35,s:98,glow:.70,speed:.18},
  thinking:{h:34,s:100,glow:1,speed:1.05},
  listening:{h:40,s:96,glow:.85,speed:.43},
  speaking:{h:29,s:100,glow:.95,speed:.65},
  warn:{h:25,s:100,glow:.9,speed:.6},
  error:{h:0,s:90,glow:.9,speed:.35},
  offline:{h:35,s:32,glow:.24,speed:.06}
};
export class ArcReactor {
  constructor(canvas) {
    this.canvas=canvas; this.ctx=canvas.getContext('2d');
    this.phase=0; this.pulse=0; this.raf=0; this.running=false;
    this.state={mode:'offline',label:''};
    this._reduced=window.matchMedia('(prefers-reduced-motion: reduce)');
    this._onResize=()=>this.resize();
    window.addEventListener('resize',this._onResize);
    this.resize();
  }
  setMode(mode){this.state.mode=mode in PALETTES?mode:'offline'; if(this._reduced.matches)this.draw(0);}
  resize(){const dpr=Math.min(window.devicePixelRatio||1,2);const rect=this.canvas.getBoundingClientRect();const size=Math.max(160,Math.round(rect.width||300));this.canvas.width=size*dpr;this.canvas.height=size*dpr;this.ctx.setTransform(dpr,0,0,dpr,0,0);this.size=size;if(!this.running)this.draw(0);}
  start(){if(this.running)return;this.running=true;if(this._reduced.matches){this.draw(0);return;}const tick=()=>{if(!this.running)return;const p=PALETTES[this.state.mode]||PALETTES.offline;this.phase+=p.speed*.016;this.pulse+=.016;this.draw(this.pulse);this.raf=requestAnimationFrame(tick);};this.raf=requestAnimationFrame(tick);}
  stop(){this.running=false;cancelAnimationFrame(this.raf);}
  destroy(){this.stop();window.removeEventListener('resize',this._onResize);}
  draw(t){
    const ctx=this.ctx,S=this.size,c=S/2,p=PALETTES[this.state.mode]||PALETTES.offline;
    const rgba=(a,l=63)=>`hsla(${p.h},${p.s}%,${l}%,${a})`;
    ctx.clearRect(0,0,S,S);
    const breath=this._reduced.matches?.5:.5+.5*Math.sin(t*1.16);
    const gl=p.glow*(.82+.18*breath);
    // Understated halo, no grids, radar, ticks or triangle.
    const halo=ctx.createRadialGradient(c,c,c*.29,c,c,c*.97);
    halo.addColorStop(0,rgba(0));halo.addColorStop(.54,rgba(.11*gl,45));halo.addColorStop(.78,rgba(.055*gl));halo.addColorStop(1,rgba(0));
    ctx.fillStyle=halo;ctx.beginPath();ctx.arc(c,c,c*.98,0,Math.PI*2);ctx.fill();
    const drawArc=(radius,start,end,width,alpha,blur=0)=>{
      ctx.beginPath();ctx.arc(c,c,radius,start,end);ctx.lineWidth=width;
      ctx.strokeStyle=rgba(alpha*gl);ctx.shadowColor=rgba(.85*gl,58);ctx.shadowBlur=blur;ctx.lineCap='round';ctx.stroke();ctx.shadowBlur=0;
    };
    // Thin orbital tracks and two golden arcs, moving with state.
    drawArc(c*.76,0,Math.PI*2,1,.20);
    drawArc(c*.57,0,Math.PI*2,1,.16);
    drawArc(c*.86,0,Math.PI*2,.8,.14);
    for(let i=0;i<3;i++){
      const a=this.phase*(i%2?-1:1)+i*2.15;
      drawArc(c*(.61+i*.09),a,a+1.25+i*.12,1.9-i*.2,.73,13);
    }
    // Glowing traveling fragments, sparse enough to keep central text readable.
    for(let i=0;i<38;i++){
      const angle=i*2.39996 + this.phase*(.7+(i%3)*.14);
      const radius=c*(.54+(i%11)*.031);
      const x=c+Math.cos(angle)*radius,y=c+Math.sin(angle)*radius;
      const r=i%8===0?1.55:.65;
      ctx.beginPath();ctx.fillStyle=rgba((i%7===0?.80:.30)*gl,72);ctx.arc(x,y,r,0,Math.PI*2);ctx.fill();
    }
    // A few warmer inner rings, and a clear dark status center.
    drawArc(c*.39,-.15+this.phase*.3,Math.PI*1.5+this.phase*.3,1.35,.46,7);
    drawArc(c*.48,1.25-this.phase*.26,4.65-this.phase*.26,1.1,.33,5);
    const inner=ctx.createRadialGradient(c,c,c*.08,c,c,c*.4);
    inner.addColorStop(0,'rgba(12,8,5,.98)');inner.addColorStop(.8,'rgba(12,8,5,.96)');inner.addColorStop(1,'rgba(12,8,5,0)');
    ctx.fillStyle=inner;ctx.beginPath();ctx.arc(c,c,c*.4,0,Math.PI*2);ctx.fill();
    ctx.beginPath();ctx.arc(c,c,c*.34,0,Math.PI*2);ctx.lineWidth=.8;ctx.strokeStyle=rgba(.27*gl);ctx.stroke();
  }
}

/* ── sparkline ──────────────────────────────────────────────────────────── */
export class Sparkline {
  constructor(canvas, { max = 100, color = "#3ce0ff", fill = true } = {}) {
    this.canvas = canvas;
    this.ctx = canvas.getContext("2d");
    this.max = max;
    this.color = color;
    this.fill = fill;
    this.values = [];
    this._onResize = () => this.resize();
    window.addEventListener("resize", this._onResize);
    this.resize();
  }

  resize() {
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    const rect = this.canvas.getBoundingClientRect();
    const w = Math.max(60, Math.round(rect.width || 200));
    const h = Math.max(20, Math.round(rect.height || 34));
    this.canvas.width = w * dpr;
    this.canvas.height = h * dpr;
    this.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    this.w = w; this.h = h;
    this.draw();
  }

  push(value) {
    if (typeof value !== "number" || Number.isNaN(value)) return;
    this.values.push(value);
    if (this.values.length > 120) this.values.shift();
    this.draw();
  }

  setAll(values) {
    this.values = Array.isArray(values) ? values.slice(-120) : [];
    this.draw();
  }

  clear() { this.values = []; this.draw(); }

  destroy() { window.removeEventListener("resize", this._onResize); }

  draw() {
    const { ctx, w, h } = this;
    if (!w || !h) return;
    ctx.clearRect(0, 0, w, h);
    const vals = this.values;
    if (vals.length < 2) {
      ctx.strokeStyle = "rgba(97,116,142,0.5)";
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(0, h - 2);
      ctx.lineTo(w, h - 2);
      ctx.stroke();
      return;
    }
    const n = vals.length;
    const stepX = w / (n - 1);
    const toY = (v) => h - 2 - Math.min(1, Math.max(0, v / this.max)) * (h - 4);
    ctx.beginPath();
    for (let i = 0; i < n; i++) {
      const x = i * stepX;
      const y = toY(vals[i]);
      i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
    }
    ctx.strokeStyle = this.color;
    ctx.lineWidth = 1.5;
    ctx.lineJoin = "round";
    ctx.stroke();
    if (this.fill) {
      ctx.lineTo(w, h);
      ctx.lineTo(0, h);
      ctx.closePath();
      const grad = ctx.createLinearGradient(0, 0, 0, h);
      grad.addColorStop(0, hexA(this.color, 0.28));
      grad.addColorStop(1, hexA(this.color, 0));
      ctx.fillStyle = grad;
      ctx.fill();
    }
  }
}

function hexA(hex, a) {
  const m = /^#?([0-9a-f]{6})$/i.exec(hex);
  if (!m) return hex;
  const int = parseInt(m[1], 16);
  const r = (int >> 16) & 255, g = (int >> 8) & 255, b = int & 255;
  return `rgba(${r},${g},${b},${a})`;
}
