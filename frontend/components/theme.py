"""Visual design system for the ProofLoop dashboard (pure CSS, no external assets)."""

from __future__ import annotations

import base64

import streamlit as st

CSS = """
<style>
:root {
  --pl-ink: #0f172a; --pl-ink-2: #334155; --pl-muted: #64748b; --pl-faint: #94a3b8;
  --pl-border: #e3e7ef; --pl-card: #ffffff; --pl-bg: #f6f7fb;
  --pl-primary: #4f46e5; --pl-primary-2: #7c3aed; --pl-primary-soft: #eef2ff;
  --pl-ok: #047857; --pl-ok-bg: #ecfdf5; --pl-ok-bd: #a7f3d0;
  --pl-bad: #b91c1c; --pl-bad-bg: #fef2f2; --pl-bad-bd: #fecaca;
  --pl-warn: #b45309; --pl-warn-bg: #fffbeb; --pl-warn-bd: #fde68a;
  --pl-info: #1d4ed8; --pl-info-bg: #eff6ff; --pl-info-bd: #bfdbfe;
  --pl-shadow: 0 1px 2px rgba(15,23,42,.04), 0 10px 30px -18px rgba(15,23,42,.18);
}
.block-container { padding-top: 2.2rem; padding-bottom: 4rem; max-width: 1320px; }
[data-testid="stHeader"] { background: transparent; }
h1, h2, h3 { letter-spacing: -0.02em; }

/* ---------- sidebar ---------- */
[data-testid="stSidebar"] { border-right: 1px solid #1b2340; }
.pl-brand { display:flex; align-items:center; gap:.7rem; padding:.2rem .2rem 1rem .2rem; }
.pl-logo { width:38px; height:38px; border-radius:11px; display:grid; place-items:center;
  background: linear-gradient(135deg,#6366f1 0%,#8b5cf6 55%,#22d3ee 100%); box-shadow:0 6px 18px -6px rgba(99,102,241,.8); }
.pl-logo svg, .pl-logo img { width:22px; height:22px; }
.pl-brand-name { font-weight:800; font-size:1.18rem; color:#f8fafc; letter-spacing:-.02em; line-height:1.1; }
.pl-brand-tag { font-size:.72rem; color:#94a3c4; line-height:1.2; }
.pl-side-card { background:#121933; border:1px solid #232b4a; border-radius:14px; padding:.75rem .85rem; margin:.4rem 0 .6rem 0; }
.pl-side-user { display:flex; align-items:center; gap:.6rem; }
.pl-avatar { width:34px; height:34px; border-radius:50%; display:grid; place-items:center; font-weight:700; color:#fff;
  background:linear-gradient(135deg,#4f46e5,#7c3aed); font-size:.85rem; flex-shrink:0; }
.pl-side-name { color:#eef2ff; font-weight:650; font-size:.9rem; line-height:1.15; }
.pl-side-role { color:#94a3c4; font-size:.72rem; text-transform:uppercase; letter-spacing:.06em; }
.pl-side-llm { display:flex; align-items:center; gap:.5rem; font-size:.78rem; color:#c7d2fe; }
.pl-dot { width:8px; height:8px; border-radius:50%; display:inline-block; flex-shrink:0; }
.pl-dot.on { background:#34d399; box-shadow:0 0 0 3px rgba(52,211,153,.18); }
.pl-dot.off { background:#f59e0b; box-shadow:0 0 0 3px rgba(245,158,11,.18); }
.pl-side-foot { color:#7782a6; font-size:.7rem; margin-top:.6rem; line-height:1.4; }

/* ---------- page header & hero ---------- */
.pl-page-head { display:flex; align-items:flex-end; justify-content:space-between; gap:1rem; margin-bottom:1.1rem; }
.pl-eyebrow { font-size:.74rem; font-weight:700; letter-spacing:.12em; text-transform:uppercase; color:var(--pl-primary); }
.pl-title { font-size:1.95rem; font-weight:800; color:var(--pl-ink); letter-spacing:-.03em; margin:.1rem 0 .15rem 0; line-height:1.15; }
.pl-sub { color:var(--pl-muted); font-size:.98rem; max-width:860px; }
.pl-hero { position:relative; overflow:hidden; border-radius:22px; padding:1.7rem 1.9rem; margin-bottom:1.3rem; color:#fff;
  background: radial-gradient(1200px 400px at 85% -20%, rgba(34,211,238,.35), transparent 60%),
              linear-gradient(120deg,#1e1b4b 0%,#3730a3 45%,#6d28d9 100%);
  box-shadow: 0 24px 50px -28px rgba(55,48,163,.7); }
.pl-hero h1 { color:#fff; font-size:1.85rem; font-weight:800; margin:0 0 .35rem 0; letter-spacing:-.03em; }
.pl-hero p { color:#dbe1ff; margin:0; font-size:1.02rem; max-width:760px; }
.pl-hero .pl-hero-quote { margin-top:1rem; display:inline-flex; gap:.5rem; align-items:center; background:rgba(255,255,255,.12);
  border:1px solid rgba(255,255,255,.22); padding:.45rem .8rem; border-radius:999px; font-size:.86rem; color:#fff; }

/* ---------- cards & KPIs ---------- */
.pl-card { background:var(--pl-card); border:1px solid var(--pl-border); border-radius:16px; padding:1.05rem 1.2rem; box-shadow:var(--pl-shadow); }
.pl-card + .pl-card { margin-top:.8rem; }
.pl-card h4 { margin:0 0 .5rem 0; font-size:.95rem; color:var(--pl-ink); font-weight:700; }
.pl-card-title { font-size:.78rem; font-weight:700; color:var(--pl-muted); text-transform:uppercase; letter-spacing:.08em; margin-bottom:.55rem; }
.pl-kpi { background:var(--pl-card); border:1px solid var(--pl-border); border-radius:16px; padding:1rem 1.1rem; box-shadow:var(--pl-shadow); height:100%; position:relative; overflow:hidden; }
.pl-kpi::before { content:""; position:absolute; inset:0 auto 0 0; width:4px; background:var(--tone, var(--pl-primary)); }
.pl-kpi-label { font-size:.74rem; color:var(--pl-muted); font-weight:700; text-transform:uppercase; letter-spacing:.07em; }
.pl-kpi-value { font-size:1.9rem; font-weight:800; color:var(--pl-ink); letter-spacing:-.03em; line-height:1.15; margin-top:.25rem; }
.pl-kpi-sub { font-size:.8rem; color:var(--pl-muted); margin-top:.15rem; }

/* ---------- chips & badges ---------- */
.pl-chip { display:inline-flex; align-items:center; gap:.3rem; padding:.16rem .55rem; border-radius:999px; font-size:.74rem; font-weight:650;
  border:1px solid var(--bd, #e2e8f0); background:var(--bg, #f8fafc); color:var(--fg, #334155); white-space:nowrap; line-height:1.45; vertical-align:middle; }
.pl-chip.mono { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-weight:600; }
.pl-badge { display:inline-flex; align-items:center; gap:.4rem; padding:.28rem .7rem .28rem .35rem; border-radius:999px; font-weight:750; font-size:.8rem;
  letter-spacing:.03em; border:1px solid var(--bd); background:var(--bg); color:var(--fg); }
.pl-badge .ico { width:18px; height:18px; border-radius:50%; display:grid; place-items:center; color:#fff; background:var(--fg); font-size:.7rem; font-weight:900; }

/* ---------- verdict banner ---------- */
.pl-verdict { border-radius:20px; padding:1.3rem 1.5rem; border:1px solid var(--bd); background:var(--bg); display:flex; gap:1.2rem; align-items:flex-start;
  box-shadow: 0 18px 40px -30px var(--fg); margin-bottom:1rem; }
.pl-verdict-ico { width:58px; height:58px; border-radius:18px; flex-shrink:0; display:grid; place-items:center; font-size:1.7rem; font-weight:900; color:#fff;
  background:var(--fg); box-shadow:0 10px 24px -10px var(--fg); }
.pl-verdict-label { font-size:.74rem; font-weight:800; letter-spacing:.14em; text-transform:uppercase; color:var(--fg); }
.pl-verdict-title { font-size:1.6rem; font-weight:850; color:var(--pl-ink); letter-spacing:-.03em; line-height:1.15; margin:.1rem 0 .35rem 0; }
.pl-verdict-summary { color:var(--pl-ink-2); font-size:1rem; line-height:1.5; }
.pl-verdict-meta { margin-top:.6rem; display:flex; flex-wrap:wrap; gap:.4rem; }

/* ---------- claim vs reality ---------- */
.pl-vs { display:grid; grid-template-columns: 1fr auto 1fr; gap:.8rem; align-items:stretch; margin-bottom:1rem; }
.pl-vs-mid { display:grid; place-items:center; color:var(--pl-faint); font-weight:800; font-size:.8rem; letter-spacing:.1em; }
.pl-quote { font-size:1rem; color:var(--pl-ink); border-left:3px solid var(--pl-border); padding:.15rem 0 .15rem .75rem; margin:.3rem 0 .6rem 0; font-style:italic; }

/* ---------- checks ---------- */
.pl-check { display:flex; gap:.75rem; padding:.7rem .2rem; border-bottom:1px dashed var(--pl-border); }
.pl-check:last-child { border-bottom:none; }
.pl-check-ico { width:24px; height:24px; border-radius:8px; flex-shrink:0; display:grid; place-items:center; font-size:.8rem; font-weight:900; color:#fff; background:var(--fg); margin-top:.1rem; }
.pl-check-name { font-weight:680; color:var(--pl-ink); font-size:.93rem; }
.pl-check-exp { color:var(--pl-ink-2); font-size:.87rem; margin-top:.1rem; line-height:1.45; }
.pl-check-meta { margin-top:.3rem; display:flex; gap:.35rem; flex-wrap:wrap; }

/* ---------- timeline ---------- */
.pl-tl { position:relative; padding-left:1.6rem; }
.pl-tl::before { content:""; position:absolute; left:.55rem; top:.4rem; bottom:.4rem; width:2px; background:linear-gradient(#c7d2fe,#e2e8f0); }
.pl-tl-item { position:relative; margin-bottom:.75rem; }
.pl-tl-dot { position:absolute; left:-1.42rem; top:.85rem; width:14px; height:14px; border-radius:50%; background:#fff; border:3px solid var(--fg, #6366f1); }
.pl-tl-body { background:var(--pl-card); border:1px solid var(--pl-border); border-radius:14px; padding:.65rem .85rem; box-shadow:var(--pl-shadow); }
.pl-tl-head { display:flex; flex-wrap:wrap; gap:.45rem; align-items:center; }
.pl-tl-type { font-size:.7rem; font-weight:800; letter-spacing:.1em; text-transform:uppercase; color:var(--fg, #4f46e5); }
.pl-tl-time { margin-left:auto; color:var(--pl-faint); font-size:.74rem; font-family: ui-monospace, Menlo, Consolas, monospace; }
.pl-tl-msg { color:var(--pl-ink-2); font-size:.88rem; margin-top:.25rem; }
.pl-code { background:#0b1022; color:#dbe2f3; border-radius:10px; padding:.55rem .7rem; font-size:.76rem; margin:.45rem 0 0 0; max-height:180px; overflow:auto;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; white-space:pre-wrap; word-break:break-word; }

/* ---------- misc ---------- */
.pl-empty { text-align:center; padding:2.4rem 1rem; border:1.5px dashed #cfd6e4; border-radius:18px; background:#fbfcfe; color:var(--pl-muted); }
.pl-empty .big { font-size:2rem; margin-bottom:.3rem; }
.pl-empty h4 { color:var(--pl-ink); margin:.2rem 0 .3rem 0; font-size:1.05rem; }
.pl-kv { display:grid; grid-template-columns: max-content 1fr; gap:.3rem .9rem; font-size:.88rem; }
.pl-kv .k { color:var(--pl-muted); }
.pl-kv .v { color:var(--pl-ink); font-weight:560; word-break:break-word; }
.pl-scn { background:var(--pl-card); border:1px solid var(--pl-border); border-radius:16px; padding:1rem 1.05rem .7rem 1.05rem; box-shadow:var(--pl-shadow);
  height:250px; display:flex; flex-direction:column; gap:.4rem; transition:transform .12s ease, box-shadow .12s ease; overflow:hidden; }
.pl-scn:hover { transform: translateY(-2px); box-shadow: 0 16px 34px -22px rgba(79,70,229,.55); }
.pl-scn-title { font-weight:760; color:var(--pl-ink); font-size:1rem; letter-spacing:-.01em; }
.pl-scn-desc { color:var(--pl-muted); font-size:.85rem; line-height:1.4; display:-webkit-box; -webkit-line-clamp:2; -webkit-box-orient:vertical; overflow:hidden; }
.pl-scn-instr { font-size:.82rem; color:var(--pl-ink-2); background:#f6f7fb; border:1px solid var(--pl-border); border-radius:10px; padding:.4rem .55rem; font-family: ui-monospace, Menlo, Consolas, monospace;
  display:-webkit-box; -webkit-line-clamp:3; -webkit-box-orient:vertical; overflow:hidden; }
.pl-req { display:flex; gap:.5rem; align-items:flex-start; font-size:.88rem; color:var(--pl-ink-2); padding:.22rem 0; }
.pl-req .n { width:20px; height:20px; border-radius:6px; background:var(--pl-primary-soft); color:var(--pl-primary); font-weight:800; font-size:.7rem; display:grid; place-items:center; flex-shrink:0; }
.pl-login-wrap { max-width: 440px; margin: 4vh auto 0 auto; }
.pl-login-brand { text-align:center; margin-bottom:1.3rem; }
.pl-login-brand .pl-logo { width:62px; height:62px; border-radius:18px; margin:0 auto .8rem auto; }
.pl-login-brand .pl-logo svg, .pl-login-brand .pl-logo img { width:34px; height:34px; }
.pl-login-title { font-size:1.9rem; font-weight:850; letter-spacing:-.035em; color:var(--pl-ink); }
.pl-login-tag { color:var(--pl-muted); font-size:.95rem; }
.pl-foot-note { text-align:center; color:var(--pl-faint); font-size:.78rem; margin-top:1rem; }
.pl-target { display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:.35rem; padding:.5rem 0; border-bottom:1px dashed var(--pl-border); font-size:.9rem; }
.pl-target:last-child { border-bottom:none; }
div[data-testid="stMetric"] { background:var(--pl-card); border:1px solid var(--pl-border); border-radius:14px; padding:.7rem .9rem; box-shadow:var(--pl-shadow); }
div[data-testid="stExpander"] details { border-radius:14px; }
button[kind="primary"] { box-shadow: 0 8px 20px -12px rgba(79,70,229,.9); }
</style>
"""

_SVG = (
    '<svg viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">'
    '<path d="M12 2.8l7.2 3v5.4c0 4.6-3 8.5-7.2 10-4.2-1.5-7.2-5.4-7.2-10V5.8L12 2.8z" stroke="white" stroke-width="1.8" stroke-linejoin="round"/>'
    '<path d="M8.4 12.2l2.5 2.5 4.8-5.2" stroke="white" stroke-width="2.1" stroke-linecap="round" stroke-linejoin="round"/></svg>'
)
# Streamlit's HTML sanitiser strips inline <svg>; an <img> data URI survives it.
LOGO_SVG = '<img alt="" src="data:image/svg+xml;base64,' + base64.b64encode(_SVG.encode()).decode() + '"/>'


def inject_css() -> None:
    st.html(CSS)
