#!/usr/bin/env python3.12

from __future__ import annotations

from flask import render_template_string

CSS = """
:root{
  --bg:#04060a;--bg2:#070b12;
  --panel:#090d14;--panel2:#0d1420;--panel3:#121a2a;
  --line:#16202e;--line2:#223048;--line3:#31456b;
  --txt:#e8eef7;--dim:#7d8ba0;--dim2:#556278;
  --blue:#4da6ff;--blue2:#2b7fd9;--blue3:#0d2240;
  --red:#ff5c5c;--red2:#b91c1c;--red3:#3a0d0d;
  --yellow:#eab308;
  --glow:0 0 18px rgba(77,166,255,.16);
  --glow-strong:0 0 34px rgba(77,166,255,.32);
  --r:0px;
}
*{box-sizing:border-box;margin:0;padding:0}
html,body{
  background:
    radial-gradient(ellipse 900px 500px at 12% -5%, rgba(77,166,255,.07), transparent 60%),
    radial-gradient(ellipse 800px 500px at 95% 105%, rgba(77,166,255,.06), transparent 60%),
    var(--bg);
  color:var(--txt);min-height:100vh;
  font:13px/1.5 "Inter","Segoe UI",system-ui,-apple-system,Arial,sans-serif;
  -webkit-font-smoothing:antialiased;
}
a{color:var(--blue);text-decoration:none}
.mono{font-family:ui-monospace,"SF Mono",Consolas,monospace}
.up{color:var(--blue)}.down{color:var(--red)}.mut{color:var(--dim)}
.empty{color:var(--dim);font-size:12px;padding:14px;text-align:center}
.err{color:var(--red);font-size:13px;padding:16px}
::-webkit-scrollbar{width:9px;height:9px}
::-webkit-scrollbar-thumb{background:var(--line2);border-radius:6px}
::-webkit-scrollbar-thumb:hover{background:var(--line3)}
::-webkit-scrollbar-track{background:transparent}

/* ---------- layout: sidebar + main ---------- */
.layout{display:grid;grid-template-columns:216px 1fr;min-height:100vh}
.sidebar{
  background:linear-gradient(180deg,var(--panel2),var(--panel));
  border-right:1px solid var(--line);
  padding:18px 0 14px;display:flex;flex-direction:column;gap:2px;
  position:sticky;top:0;height:100vh;overflow-y:auto;
}
.sb-brand{padding:0 18px 4px;display:flex;align-items:center;gap:10px;
  font-size:14px;font-weight:700;letter-spacing:2.5px;color:#fff;white-space:nowrap}
.sb-brand b{color:var(--blue)}
.sb-brand .logo{width:26px;height:26px;border-radius:0px;flex:none;
  background:linear-gradient(135deg,var(--blue),var(--blue));
  box-shadow:var(--glow-strong)}
.sb-sub{padding:2px 18px 14px;font-size:9px;letter-spacing:2.5px;color:var(--dim2);
  text-transform:uppercase}
.sb-section{font-size:9.5px;letter-spacing:2.2px;color:var(--dim2);
  text-transform:uppercase;padding:14px 18px 6px;font-weight:600}
.sb-item{display:flex;align-items:center;gap:10px;margin:0 8px;padding:8px 10px;
  color:var(--dim);font-size:12.5px;border-radius:0px;cursor:pointer;
  border:1px solid transparent;transition:all .12s}
.sb-item:hover{color:#fff;background:var(--panel3)}
.sb-item.on{color:#fff;background:linear-gradient(90deg,var(--blue3),var(--panel3));
  border-color:var(--line2);box-shadow:inset 0 0 24px rgba(77,166,255,.06)}
.sb-item .ic{width:16px;text-align:center;font-size:13px;color:var(--blue)}
.sb-item .badge{margin-left:auto;background:var(--blue3);color:var(--blue);
  font-size:10px;padding:1px 8px;border-radius:0px;font-weight:600;
  font-family:ui-monospace,Consolas,monospace}
.sb-item .badge.off{background:var(--red3);color:var(--red)}
.sb-foot{margin-top:auto;padding:12px 18px 0;font-size:10px;color:var(--dim2);
  letter-spacing:1px}

/* ---------- main column ---------- */
.main{min-width:0}
.topbar{display:flex;align-items:center;gap:18px;padding:0 22px;height:56px;
  background:rgba(11,15,12,.82);backdrop-filter:blur(8px);
  border-bottom:1px solid var(--line);position:sticky;top:0;z-index:30}
.page-title{font-size:15px;font-weight:700;letter-spacing:3px;color:#fff}
.page-sub{font-size:10.5px;color:var(--dim);letter-spacing:1px;margin-top:1px}
.spacer{flex:1}
.status-pill{display:flex;align-items:center;gap:8px;padding:6px 14px;
  border:1px solid var(--line2);border-radius:0px;background:var(--panel2);
  font-size:10.5px;letter-spacing:1.8px;color:var(--dim);font-weight:600}
.status-pill .dot{width:8px;height:8px;border-radius:0px;background:var(--blue);
  box-shadow:0 0 10px var(--blue);animation:pulse 2.4s infinite}
.status-pill.bad .dot{background:var(--red);box-shadow:0 0 10px var(--red);
  animation:none}
.status-pill.bad{color:var(--red)}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.45}}
#clock{color:var(--dim);font-size:12px;letter-spacing:1px}

/* ---------- content ---------- */
.wrap{padding:18px 22px;max-width:1720px;margin:0 auto}

/* KPI cards */
.kpi-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(178px,1fr));
  gap:12px;margin-bottom:16px}
.kpi{background:linear-gradient(160deg,var(--panel2),var(--panel));
  border:1px solid var(--line);border-radius:0px;padding:14px 16px;
  position:relative;overflow:hidden;transition:border-color .15s}
.kpi:hover{border-color:var(--line3)}
.kpi::before{content:"";position:absolute;top:0;left:0;right:0;height:1px;
  background:linear-gradient(90deg,transparent,var(--blue),transparent);opacity:.4}
.kpi .k{font-size:9.5px;letter-spacing:1.8px;color:var(--dim);text-transform:uppercase}
.kpi .v{font-size:23px;font-weight:600;margin-top:7px;
  font-family:ui-monospace,Consolas,monospace;letter-spacing:-.5px;color:#fff}
.kpi .v.up{color:var(--blue);text-shadow:0 0 16px rgba(77,166,255,.35)}
.kpi .v.down{color:var(--red);text-shadow:0 0 16px rgba(255,92,92,.3)}
.kpi .sub{font-size:10.5px;color:var(--dim);margin-top:5px}

/* panels + sections */
.grid{display:grid;gap:14px}
.accounts{grid-template-columns:1fr 1fr;align-items:start}
.accounts.mid{grid-template-columns:1fr auto 1fr;align-items:stretch}
.midclose{display:flex;align-items:center;justify-content:center;padding:6px 2px}
.btn.big{padding:6px 12px;font-size:10px;font-weight:700;letter-spacing:.14em;
  line-height:1.2;background:linear-gradient(160deg,#2a0e0e,#180808);
  border:1px solid #5a1a1a;color:#ffb4b4;border-radius:12px;white-space:nowrap;
  box-shadow:0 1px 6px rgba(0,0,0,.5),inset 0 1px 0 rgba(255,92,92,.15);
  transition:transform .08s ease,box-shadow .08s ease,background .08s ease,
  border-color .08s ease,color .08s ease;cursor:pointer}
.btn.big:hover{border-color:#ff5c5c;color:#fff;
  background:linear-gradient(160deg,#3a1212,#220a0a);
  box-shadow:0 0 12px rgba(255,92,92,.28),inset 0 1px 0 rgba(255,92,92,.28);
  transform:translateY(-1px)}
.btn.big:active{transform:scale(.94);
  box-shadow:0 0 6px rgba(255,92,92,.35),inset 0 1px 0 rgba(255,92,92,.3)}
.btn.big:disabled{opacity:.55;cursor:progress;transform:none}
.btn.big.pop{animation:pop .18s ease}
.btn.close-top{display:inline-flex;align-items:center;gap:6px;padding:6px 16px;
  font-size:11px;font-weight:700;letter-spacing:.12em;line-height:1.2;
  background:linear-gradient(160deg,#2a0e0e,#180808);
  border:1px solid #5a1a1a;color:#ffb4b4;border-radius:9px;white-space:nowrap;
  box-shadow:0 1px 6px rgba(0,0,0,.5),inset 0 1px 0 rgba(255,92,92,.15);
  transition:transform .08s ease,box-shadow .08s ease,background .08s ease,
  border-color .08s ease,color .08s ease;cursor:pointer}
.btn.close-top:hover{border-color:#ff5c5c;color:#fff;
  background:linear-gradient(160deg,#3a1212,#220a0a);
  box-shadow:0 0 12px rgba(255,92,92,.28),inset 0 1px 0 rgba(255,92,92,.28);
  transform:translateY(-1px)}
.btn.close-top:active{transform:scale(.94);
  box-shadow:0 0 6px rgba(255,92,92,.35),inset 0 1px 0 rgba(255,92,92,.3)}
.btn.close-top:disabled{opacity:.55;cursor:progress;transform:none}
/* big solid-red CLOSE bar above the ACCOUNT 1 panel */
.closebar{display:flex;justify-content:flex-start;margin-bottom:10px}
.btn.close-main{display:inline-flex;align-items:center;justify-content:center;
  gap:8px;padding:10px 30px;font-size:13px;font-weight:800;
  letter-spacing:.16em;line-height:1.2;
  background:linear-gradient(160deg,#ef4444,#b91c1c);
  border:1px solid #ff5c5c;color:#fff;border-radius:10px;white-space:nowrap;
  cursor:pointer;box-shadow:0 0 18px rgba(255,92,92,.35),0 2px 10px rgba(0,0,0,.5);
  transition:transform .08s ease,box-shadow .08s ease,filter .08s ease}
.btn.close-main:hover{filter:brightness(1.12);
  box-shadow:0 0 26px rgba(255,92,92,.5),0 2px 10px rgba(0,0,0,.5);
  transform:translateY(-1px)}
.btn.close-main:active{transform:scale(.95)}
.btn.close-main:disabled{opacity:.6;cursor:progress;transform:none}
@keyframes pop{0%{transform:scale(1)}40%{transform:scale(.9)}100%{transform:scale(1)}}
/* small cool ORDER button (replaces the old full-width block) */
.btn.order-mini{display:inline-flex;align-items:center;gap:6px;padding:5px 13px;
  font-size:10px;font-weight:700;letter-spacing:.14em;border-radius:11px;
  background:linear-gradient(160deg,#0e1e33,#0a1220);border:1px solid #23456b;
  color:#7db9ff;cursor:pointer;white-space:nowrap;
  box-shadow:0 1px 6px rgba(0,0,0,.4),inset 0 1px 0 rgba(77,166,255,.18);
  transition:transform .08s ease,box-shadow .08s ease,border-color .08s ease,
  color .08s ease;width:auto;max-width:230px}
.btn.order-mini:hover{border-color:var(--blue);color:#fff;
  box-shadow:0 0 12px rgba(77,166,255,.32),inset 0 1px 0 rgba(77,166,255,.3);
  transform:translateY(-1px)}
.btn.order-mini:active{transform:scale(.94)}
.btn.order-mini:disabled{opacity:.55;cursor:progress;transform:none}
.btn.order-mini.pop{animation:pop .18s ease}
/* NEWS sits beside ORDER / SCHEDULE / STOPEXE and is deliberately a notch
   bigger: it opens the week-of-events panel, not a single order. */
.btn.order-mini.news{padding:9px 24px;font-size:12px;letter-spacing:.18em;
  border-radius:13px;border-color:#8a6d1f;color:#ffd479;
  background:linear-gradient(160deg,#2a2109,#161005);
  box-shadow:0 1px 8px rgba(0,0,0,.45),0 0 14px rgba(255,196,77,.16),
  inset 0 1px 0 rgba(255,196,77,.25)}
.btn.order-mini.news:hover{border-color:#ffc44d;color:#fff;
  box-shadow:0 0 22px rgba(255,196,77,.4),inset 0 1px 0 rgba(255,196,77,.35)}
.order-wrap{display:flex;justify-content:center;margin-top:10px}
@media(max-width:1180px){.accounts{grid-template-columns:1fr}
  .accounts.mid{grid-template-columns:1fr}
  .midclose{padding:8px 0}
  .btn.big{padding:7px 14px;width:auto}}
.panel{background:linear-gradient(180deg,var(--panel2),var(--panel));
  border:1px solid var(--line);border-radius:0px;overflow:hidden;
  box-shadow:0 8px 30px rgba(0,0,0,.35)}
.sect{display:flex;align-items:center;gap:10px;padding:9px 14px;
  border-bottom:1px solid var(--line);border-top:1px solid var(--line);
  background:rgba(18,26,42,.6)}
.sect h4{font-size:10px;letter-spacing:2px;color:var(--dim);
  text-transform:uppercase;font-weight:600}
.sect h4::before{content:"◆ ";color:var(--blue);font-size:8px}
.sect .right{margin-left:auto;display:flex;gap:6px;align-items:center}
.body{padding:13px}
.acc-head{display:flex;flex-wrap:wrap;align-items:center;gap:8px 14px;
  padding:12px 16px;border-bottom:1px solid var(--line);
  background:linear-gradient(90deg,var(--blue3),transparent 70%)}
.acc-head .name{font-size:13px;font-weight:700;letter-spacing:2px;color:#fff}
.acc-head .sub{color:var(--dim);font-size:11px}
.dot{display:inline-block;width:8px;height:8px;border-radius:0px;
  background:var(--blue);box-shadow:0 0 8px var(--blue)}
.dot.warn{background:var(--yellow);box-shadow:0 0 8px var(--yellow)}
.dot.off{background:var(--red);box-shadow:0 0 8px var(--red)}

/* chips */
.chips{display:flex;flex-wrap:wrap;gap:6px}
.chip{border:1px solid var(--line2);background:var(--panel3);
  padding:3px 10px;font-size:10.5px;color:var(--dim);letter-spacing:.5px;
  border-radius:0px}
.chip b{color:var(--blue);font-weight:600;margin-left:5px;
  font-family:ui-monospace,Consolas,monospace}

/* kv stat blocks */
.kv{display:grid;grid-template-columns:repeat(auto-fit,minmax(126px,1fr));gap:10px}
.kv>div{background:var(--panel);border:1px solid var(--line);
  border-radius:0px;padding:10px 12px}
.k{font-size:9px;letter-spacing:1.7px;color:var(--dim);text-transform:uppercase}
.v{font-size:18px;font-weight:600;margin-top:4px;
  font-family:ui-monospace,Consolas,monospace;color:#fff}
.v.sm{font-size:15px}

/* perf stats + bars */
.statgrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(118px,1fr));gap:10px}
.statgrid>div{background:var(--panel);border:1px solid var(--line);
  border-radius:0px;padding:10px 12px}
.bar{height:5px;background:var(--line);margin-top:7px;position:relative;
  border-radius:0px;overflow:hidden}
.bar>i{position:absolute;left:0;top:0;bottom:0;border-radius:0px;
  background:linear-gradient(90deg,var(--blue),var(--blue));
  box-shadow:0 0 8px rgba(77,166,255,.5)}
.pairrow{display:grid;grid-template-columns:92px 1fr 48px;gap:10px;
  align-items:center;padding:4px 0;font-size:12px}
.pairrow .bar{margin-top:0}

/* donut / ring gauge */
.ringwrap{display:flex;align-items:center;gap:14px}
.ring{position:relative;width:74px;height:74px;flex:none}
.ring svg{transform:rotate(-90deg)}
.ring .pct{position:absolute;inset:0;display:flex;align-items:center;
  justify-content:center;font-family:ui-monospace,Consolas,monospace;
  font-size:14px;font-weight:600;color:#fff}

/* buttons + inputs */
.btn{background:var(--panel3);border:1px solid var(--line2);color:var(--txt);
  padding:7px 14px;font-size:11px;letter-spacing:1.2px;cursor:pointer;
  text-transform:uppercase;font-weight:600;border-radius:0px;transition:all .12s}
.btn:hover{border-color:var(--blue);color:#fff;box-shadow:var(--glow)}
.btn:active{transform:translateY(1px)}
.btn.blue{background:var(--blue3);border-color:var(--blue3);color:var(--blue)}
.btn.danger{color:#ffb4b4}
.btn.mini{padding:3px 10px;font-size:10px;border-radius:7px}
.tradebtns{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.tbtn{padding:13px 0;font-size:16px;font-weight:700;letter-spacing:2.5px;color:#fff;
  cursor:pointer;border:1px solid transparent;border-radius:0px;transition:all .15s}
.tbtn:hover{filter:brightness(1.15);box-shadow:var(--glow-strong)}
.tbtn:active{transform:translateY(1px)}
.tbuy{background:linear-gradient(160deg,var(--blue2),var(--blue3));
  border-color:var(--blue);box-shadow:0 0 22px rgba(77,166,255,.22)}
.tsell{background:linear-gradient(160deg,var(--red2),var(--red3));
  border-color:var(--red);box-shadow:0 0 22px rgba(255,92,92,.15)}
/* MT5-style execution buttons: side label + live price (SELL=bid, BUY=ask) */
.tbtn .lbl{display:block;font-size:11px;letter-spacing:2.5px;opacity:.92}
.tbtn .px{display:block;margin-top:3px;font-size:15px;font-weight:600;
  letter-spacing:.5px;font-family:ui-monospace,Consolas,monospace}
.tbtn.off,.tbtn.off:hover{background:#20242c;border-color:#3a4150;
  color:var(--dim);box-shadow:none;cursor:not-allowed;filter:none}
.tbtn.off:active{transform:none}
.lotrow{display:flex;gap:0;align-items:stretch;border-radius:0px;overflow:hidden}
.lotrow button{width:38px;background:var(--panel3);border:1px solid var(--line2);
  color:#fff;font-size:16px;cursor:pointer}
.lotrow button:hover{border-color:var(--blue);color:var(--blue)}
.lotrow input{flex:1;background:var(--bg);border:1px solid var(--line2);
  border-left:none;border-right:none;color:#fff;text-align:center;
  padding:8px 0;font-size:15px;font-family:ui-monospace,Consolas,monospace;outline:none}
input[type=number]::-webkit-outer-spin-button,
input[type=number]::-webkit-inner-spin-button{-webkit-appearance:none;margin:0}
.lotrow input:focus{border-color:var(--blue)}
.chiprow{display:flex;flex-wrap:wrap;gap:6px;margin-top:8px}
.lchip{border:1px solid var(--line2);background:var(--panel3);color:var(--dim);
  padding:3px 11px;font-size:11px;cursor:pointer;border-radius:0px;
  font-family:ui-monospace,Consolas,monospace}
.lchip:hover,.lchip.on{border-color:var(--blue);color:var(--blue);
  box-shadow:var(--glow)}
select,input[type=text],input[type=password]{background:var(--bg);
  border:1px solid var(--line2);color:#fff;padding:7px 9px;font-size:12px;
  outline:none;width:100%;border-radius:0px}
select:focus,input[type=text]:focus,input[type=password]:focus{
  border-color:var(--blue);box-shadow:var(--glow)}
.frow{display:grid;grid-template-columns:repeat(auto-fit,minmax(88px,1fr));gap:9px}
.fgroup label{display:block;font-size:9px;letter-spacing:1.5px;
  color:var(--dim);text-transform:uppercase;margin-bottom:4px}
.fgroup{min-width:0}
.feedback{font-size:12px;min-height:18px;margin-top:8px;color:var(--dim)}
.feedback.ok{color:var(--blue)}.feedback.err{color:var(--red)}

/* modal (future-trade setup) */
.modal-bg{position:fixed;inset:0;background:rgba(2,4,8,.72);
  z-index:100;display:none;
  align-items:center;justify-content:center}
.modal-bg.open{display:flex}
.modal{background:linear-gradient(180deg,var(--panel3),var(--panel));
  border:1px solid var(--line3);border-radius:0px;
  width:min(600px,93vw);max-height:90vh;overflow-y:auto;padding:22px;
  box-shadow:var(--glow-strong),0 30px 80px rgba(0,0,0,.65)}
.modal h3{font-size:13px;letter-spacing:2.5px;margin-bottom:16px;
  text-transform:uppercase;color:#fff}
.modal h3::before{content:"◆ ";color:var(--blue)}
.modal .closex{position:absolute;top:14px;right:16px;background:none;
  border:none;color:var(--dim);font-size:18px;cursor:pointer}
.modal .closex:hover{color:var(--red)}
.modal-head{display:flex;align-items:center}
.modal-head h3{flex:1}

/* tables */
table{width:100%;border-collapse:collapse;font-size:12px}
th{color:var(--dim);font-size:9.5px;letter-spacing:1.5px;text-transform:uppercase;
  text-align:left;padding:7px 12px;border-bottom:1px solid var(--line);
  background:rgba(18,26,42,.6);white-space:nowrap}
td{padding:6px 12px;border-bottom:1px solid var(--line);white-space:nowrap}
tr:hover td{background:var(--panel2)}
td.num,th.num{text-align:right;font-family:ui-monospace,Consolas,monospace}
.tblwrap{overflow-x:auto}
.side{display:inline-block;padding:1px 9px;font-size:10px;font-weight:700;
  letter-spacing:1px;color:#fff;border-radius:0px}
.side.BUY{background:var(--blue2);box-shadow:0 0 10px rgba(77,166,255,.35)}
.side.SELL{background:var(--red2);box-shadow:0 0 10px rgba(255,92,92,.3)}
.xbtn{background:none;border:1px solid var(--line2);color:var(--dim);
  cursor:pointer;padding:1px 9px;font-size:10.5px;border-radius:0px}
.xbtn:hover{border-color:var(--red);color:var(--red)}

/* toast */
.toast{position:fixed;top:66px;right:20px;z-index:200;background:var(--panel3);
  border:1px solid var(--line3);color:var(--txt);padding:10px 16px;font-size:12px;
  max-width:420px;opacity:0;pointer-events:none;transition:opacity .15s;
  border-radius:0px;box-shadow:0 10px 40px rgba(0,0,0,.5)}
.toast.show{opacity:1}
.toast.ok{border-color:var(--blue);box-shadow:var(--glow-strong)}
.toast.err{border-color:var(--red)}

/* chart */
.chartbox{height:190px;border:1px solid var(--line);border-radius:0px;
  background:var(--bg);overflow:hidden}
.chartbox svg{display:block;width:100%;height:100%}
.legend{display:flex;gap:16px;font-size:10.5px;color:var(--dim);
  letter-spacing:1px;padding:7px 2px 0;flex-wrap:wrap}
.legend i{display:inline-block;width:14px;height:3px;border-radius:2px;
  vertical-align:middle;margin-right:5px}

/* rotating dial time picker (H/M/S circles) */
.dials{display:flex;gap:16px;flex-wrap:wrap}
.dial{position:relative;width:84px;text-align:center;cursor:grab;
  user-select:none;-webkit-user-select:none;touch-action:none}
.dial:active{cursor:grabbing}
.dial svg{width:84px;height:84px;display:block;overflow:visible}
.dial .dial-val{position:absolute;top:30px;left:0;right:0;
  font-family:ui-monospace,Consolas,monospace;font-size:18px;font-weight:700;color:#fff;
  pointer-events:none;text-shadow:0 0 12px rgba(77,166,255,.55)}
.dial .dial-lbl{margin-top:3px;font-size:9px;letter-spacing:1.6px;color:var(--dim);
  text-transform:uppercase}
.dial .tick{stroke:var(--line3);stroke-width:1.5}
.dial .tick.maj{stroke:var(--dim2);stroke-width:2}
.dial .track{fill:none;stroke:var(--line);stroke-width:3.5}
.dial .prog{fill:none;stroke:var(--blue);stroke-width:3.5;stroke-linecap:round;
  filter:drop-shadow(0 0 4px rgba(77,166,255,.55))}
.dial .arm{stroke:var(--blue2);stroke-width:1.5;opacity:.7}
.dial .knob{fill:#fff;stroke:var(--blue);stroke-width:2;
  filter:drop-shadow(0 0 6px rgba(77,166,255,.85))}
.secthint{font-size:9px;letter-spacing:2px;color:var(--dim);margin:14px 0 6px;
  text-transform:uppercase}
.secthint b{color:var(--blue)}
.cnt{margin-top:8px;font-size:11px;color:var(--blue2);font-variant-numeric:tabular-nums}
.chip.filter{cursor:pointer;user-select:none;opacity:.55}
.chip.filter.on{opacity:1;border-color:var(--blue);color:var(--blue);box-shadow:var(--glow)}
.chip.filter b{color:inherit}
.dials .btn.mini{margin:26px 0 0 6px;font-weight:700;min-width:44px;
  background:var(--blue3);color:var(--blue);border:1px solid var(--line3)}
/* ---------- symbol search (manual / order / schedule pickers) ---------- */
.symsearch{margin-bottom:6px;position:relative}
.symsearch input{background:var(--bg);border:1px solid var(--line2);
  color:#fff;padding:6px 9px;font-size:12px;outline:none;width:100%;
  border-radius:0px}
.symsearch input:focus{border-color:var(--blue);box-shadow:var(--glow)}
.symsearch input::placeholder{color:var(--dim2)}
/* ---------- StopExe straddle panel ---------- */
.stex-live{display:flex;flex-wrap:wrap;gap:6px;margin:8px 0}
.stex-spr{background:var(--bg);border:1px solid var(--line2);color:#fff;
  padding:7px 9px;font-size:12px;outline:none;width:100%;border-radius:0px;
  font-family:ui-monospace,Consolas,monospace}
.stex-spr:focus{border-color:var(--blue);box-shadow:var(--glow)}
.stex-bracket{display:flex;gap:10px;flex-wrap:wrap;margin:8px 0}
.stex-bracket>div{flex:1;min-width:140px;background:var(--panel);
  border:1px solid var(--line);border-radius:0px;padding:8px 12px}
.stex-bracket .k{font-size:9px;letter-spacing:1.7px;color:var(--dim);
  text-transform:uppercase}
.stex-bracket .v{font-size:16px;font-weight:600;margin-top:3px;
  font-family:ui-monospace,Consolas,monospace;color:#fff}
.stex-bracket .v.buy{color:var(--blue)}
.stex-bracket .v.sell{color:var(--red)}
.symlist{max-height:168px;overflow-y:auto;border:1px solid var(--line2);
  background:var(--bg);margin-top:6px;display:none}
.symlist.open{display:block}
.symlist .opt{display:flex;gap:8px;align-items:center;padding:6px 10px;
  font-size:12px;cursor:pointer;color:var(--txt)}
.symlist .opt:hover{background:var(--panel3);color:#fff}
.symlist .opt.on{background:var(--blue3);color:var(--blue)}
.symlist .opt .mono{font-family:ui-monospace,Consolas,monospace}
.symlist .opt .pr{margin-left:auto;color:var(--dim);font-size:11px;
  font-family:ui-monospace,Consolas,monospace}
.tbtn.stex{background:linear-gradient(160deg,#7c3aed,#3b0764);
  border-color:#a78bfa;box-shadow:0 0 22px rgba(167,139,250,.22);
  grid-column:span 2}
.tbtn.stex:hover{filter:brightness(1.15);box-shadow:var(--glow-strong)}
/* ---------- news panel (week of events, one release at a time) ---------- */
.news-bg{position:fixed;inset:0;z-index:120;display:none;align-items:center;
  justify-content:center;padding:18px;background:rgba(2,4,8,.84)}
.news-bg.open{display:flex}
.news-panel{width:min(1020px,96vw);max-height:94vh;overflow-y:auto;
  background:linear-gradient(180deg,var(--panel3),var(--panel));
  border:1px solid var(--line3);border-radius:0px;
  box-shadow:var(--glow-strong),0 34px 90px rgba(0,0,0,.7)}
.news-head{display:flex;align-items:center;gap:10px;padding:12px 16px;
  border-bottom:1px solid var(--line);
  background:linear-gradient(90deg,rgba(255,196,77,.10),transparent 70%)}
.news-head .title{font-size:13px;font-weight:700;letter-spacing:2.6px;
  color:#fff}
.news-head .title::before{content:"◆ ";color:#ffc44d;font-size:9px}
.news-head .sub{font-size:10px;letter-spacing:1px;color:var(--dim)}
.news-x{background:none;border:none;color:var(--dim);font-size:18px;
  cursor:pointer;padding:0 4px}
.news-x:hover{color:var(--red)}
.news-closer{display:inline-flex;align-items:center;gap:9px;padding:6px 12px;
  border:1px solid var(--line2);background:var(--panel);color:var(--dim);
  font-size:10px;font-weight:700;letter-spacing:1.5px;cursor:pointer;
  border-radius:0px;transition:all .12s}
.news-closer .knob{position:relative;width:30px;height:13px;
  border:1px solid var(--line3);background:var(--bg)}
.news-closer .knob::after{content:"";position:absolute;top:1px;left:1px;
  width:11px;height:9px;background:var(--dim2);transition:left .12s ease}
.news-closer:hover{border-color:#ffc44d;color:#ffd479}
.news-closer.on{border-color:#ffc44d;color:#ffd479;box-shadow:var(--glow)}
.news-closer.on .knob{border-color:#ffc44d}
.news-closer.on .knob::after{left:16px;background:#ffc44d;
  box-shadow:0 0 8px rgba(255,196,77,.9)}
.news-bar{display:flex;flex-wrap:wrap;align-items:center;gap:6px;
  padding:10px 16px;border-bottom:1px solid var(--line)}
.news-bar .spacer{flex:1}
.news-slide{display:grid;grid-template-columns:44px minmax(0,1fr) 44px;
  gap:10px;padding:14px 16px;align-items:stretch}
.news-nav{background:var(--panel);border:1px solid var(--line2);color:var(--blue);
  font-size:20px;font-weight:700;line-height:1;cursor:pointer;
  border-radius:0px;transition:all .12s}
.news-nav:hover{background:var(--blue3);border-color:var(--blue);
  box-shadow:var(--glow);color:#fff}
.news-nav:active{transform:scale(.96)}
.news-card{display:grid;grid-template-columns:158px minmax(0,1fr);gap:14px;
  border:1px solid var(--line);background:var(--bg);padding:12px}
@media(max-width:680px){.news-card{grid-template-columns:1fr}
  .news-slide{grid-template-columns:36px minmax(0,1fr) 36px}}
.news-map{position:relative;height:152px;background:#04070c;
  border:1px solid var(--line);display:flex;align-items:center;
  justify-content:center;overflow:hidden}
.news-map svg{display:block;width:100%;height:100%}
.news-map .ccy{position:absolute;top:6px;left:8px;font-size:9.5px;
  letter-spacing:2px;color:#ffc44d;font-weight:700}
.news-map .none{font-size:10px;color:var(--dim);padding:8px;text-align:center}
.news-when{font-family:ui-monospace,Consolas,monospace;font-size:26px;
  font-weight:700;color:#fff;letter-spacing:.5px;line-height:1.1}
.news-when .tz{font-size:11px;color:var(--dim);letter-spacing:1.5px;
  margin-left:8px;font-family:inherit;font-weight:400}
.news-metarow{display:flex;flex-wrap:wrap;gap:6px;align-items:center;
  margin:7px 0}
.news-impact{padding:2px 9px;font-size:9.5px;font-weight:700;
  letter-spacing:1.4px;text-transform:uppercase}
.news-impact.High{background:#7f1d1d;color:#ffd0d0;
  box-shadow:0 0 10px rgba(255,92,92,.25)}
.news-impact.Medium{background:#7c4a03;color:#ffe1b0}
.news-impact.Low,.news-impact.Holiday{background:#334155;color:#cbd5e1}
.news-name{font-size:14px;color:#fff;line-height:1.35}
.news-line{font-size:11px;color:var(--dim);margin-top:5px}
.news-line b{color:var(--txt);font-family:ui-monospace,Consolas,monospace;
  font-weight:600}
.news-cd{margin-top:8px;font-size:11px;letter-spacing:1px;color:var(--blue2)}
.news-cd b{color:#ffd479;font-family:ui-monospace,Consolas,monospace}
.news-controls{display:grid;gap:10px;padding:0 16px 10px;
  grid-template-columns:repeat(auto-fit,minmax(148px,1fr))}
.news-controls .seg{display:flex;gap:5px}
.news-controls .seg .btn{flex:1;padding:6px 0}
.news-controls select,.news-controls input{width:100%;background:var(--bg);
  border:1px solid var(--line2);color:#fff;padding:6px 8px;font-size:12px;
  outline:none;border-radius:0px;font-family:ui-monospace,Consolas,monospace}
.news-quote{display:flex;gap:8px;flex-wrap:wrap;align-items:center;
  padding:0 16px 10px}
.news-plans{padding:0 16px 10px;font-size:11px;color:var(--dim)}
.news-plans .prow{display:flex;gap:8px;align-items:center;flex-wrap:wrap;
  border-top:1px solid var(--line);padding:5px 0}
.news-plans .prow .st{font-size:9.5px;letter-spacing:1.2px;
  padding:1px 7px;background:var(--panel3);color:var(--blue2);
  text-transform:uppercase}
.news-plans .prow .st.bad{color:#ffb4b4}
.news-foot{display:grid;grid-template-columns:1fr 1fr;gap:10px;
  padding:0 16px 16px}
.tbtn.news-trade{background:linear-gradient(160deg,#15803d,#064e2b);
  border-color:#34d399;box-shadow:0 0 22px rgba(52,211,153,.22)}
.tbtn.news-decline{background:linear-gradient(160deg,#7f1d1d,#3f0d0d);
  border-color:#f87171;box-shadow:0 0 22px rgba(248,113,113,.18)}
/* ---------- factory (top-level page below Scheduled Trades) ---------- */
.factory-grid{display:grid;grid-template-columns:minmax(0,1fr) 318px;
  gap:14px;align-items:start}
.factory-grid>section{min-width:0}
@media(max-width:1180px){.factory-grid{grid-template-columns:1fr}}
.clockbox{position:relative;height:206px;background:#000;
  border-bottom:1px solid var(--line);overflow:hidden}
.clockbox canvas{display:block;width:100%;height:100%}
.globebox{position:relative;height:min(62vh,620px);min-height:360px;
  background:#000;overflow:hidden;touch-action:none}
.globebox canvas{display:block;width:100%;height:100%;cursor:grab}
.globebox.dragging canvas{cursor:grabbing}
.fhover{position:absolute;top:10px;left:50%;transform:translateX(-50%);
  max-width:92%;padding:5px 12px;background:rgba(5,15,35,.9);
  border:1px solid var(--line3);color:#9ec5ff;font-size:11px;
  letter-spacing:.6px;white-space:nowrap;overflow:hidden;
  text-overflow:ellipsis;pointer-events:none;z-index:2}
.fhover.hit{border-color:var(--blue)}
.fcoord{position:absolute;bottom:8px;left:12px;color:#9ec5ff;
  font-family:ui-monospace,Consolas,monospace;font-size:10px;
  pointer-events:none;z-index:2}
.fhud{position:absolute;right:10px;top:10px;text-align:right;
  color:var(--dim);font-family:ui-monospace,Consolas,monospace;
  font-size:10px;line-height:1.55;pointer-events:none;z-index:2}
.fhud b{color:var(--blue);font-weight:600}
.fnext{font-family:ui-monospace,Consolas,monospace;font-size:30px;
  font-weight:700;color:#fff;letter-spacing:1px;margin:2px 0 4px;
  text-shadow:0 0 18px rgba(77,166,255,.35)}
.fnext.live{color:#ffb4b4;text-shadow:0 0 18px rgba(255,92,92,.45)}
.falert{font-size:11px;line-height:1.75;color:#ffb3b3;
  font-family:ui-monospace,Consolas,monospace}
.falert .mut{color:var(--dim)}
.fctrl{display:flex;flex-wrap:wrap;gap:6px;margin-top:6px}
.fctrl .btn{padding:5px 10px;font-size:10px}
.fctrl .btn.on{border-color:var(--blue);color:var(--blue);
  background:var(--blue3);box-shadow:var(--glow)}
.fhoverpanel{font-size:12px;color:#9ec5ff;line-height:1.6}
.fhoverpanel .iso{color:var(--dim);font-size:10px;letter-spacing:1px}
.flog td{font-size:11.5px}
.frow-live td{background:rgba(61,0,0,.35)}
.frow-live td:first-child{color:#fff;font-weight:700}
.fsrc{font-size:10px;color:var(--dim);letter-spacing:.5px}
.fpulse{display:inline-block;width:7px;height:7px;background:var(--blue);
  box-shadow:0 0 8px var(--blue);margin-right:6px;vertical-align:middle;
  animation:pulse 2.4s infinite}
"""

COMMON_JS = """
function $id(x){return document.getElementById(x)}
function asmrPop(el){if(!el)return;el.classList.remove('pop');void el.offsetWidth;el.classList.add('pop')}
function esc(s){return String(s==null?'':s).replace(/[&<>"]/g,function(c){
 return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]})}
function fmt(v,d){if(v===null||v===undefined||isNaN(v))return '-';
  // tolerate bad digits (a class string used to land here and make
  // toLocaleString throw 'minimumFractionDigits value out of range')
  const n=parseInt(d,10);
  const digits=isFinite(n)?Math.max(0,Math.min(20,n)):2;
  return (+v).toLocaleString('en-US',{minimumFractionDigits:digits,
  maximumFractionDigits:digits})}
function signed(v,d){if(v===null||v===undefined||isNaN(v))return '-';
 return (v>0?'+':'')+fmt(v,d)}
function cls(v){return v>0?'up':(v<0?'down':'mut')}
async function api(path,opts){const r=await fetch(path,opts);
 let j;try{j=await r.json()}catch(e){throw new Error('bad response')}
 if(!r.ok||j.ok===false)throw new Error(j.error||('HTTP '+r.status));return j}
function post(path,body){return api(path,{method:'POST',
 headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})}
let TOAST_T=null;
function toast(msg,ok){const t=$id('toast');t.textContent=msg;
 t.className='toast show '+(ok?'ok':'err');clearTimeout(TOAST_T);
 TOAST_T=setTimeout(function(){t.className='toast'},3500)}
function clockTick(){$id('clock').textContent=new Date().toLocaleTimeString('en-GB')}
setInterval(clockTick,1000);clockTick();
function dot(live,warn){return live?'dot':(warn?'dot warn':'dot off')}
/* sticky link status: a single failed poll (or a 429 burst) must not flip
   the pill OFFLINE - only N consecutive failures count as a real outage.
   The old code flipped the pill on EVERY render error, so the UI flapped
   online/offline every few seconds while the bridge was perfectly fine. */
let FAILS=0;
function linkOffline(){
 FAILS++;
 if(FAILS<3)return;                       // sticky: wait for 3 consecutive
 const pill=$id('sysPill');
 if(pill){pill.className='status-pill bad';
  $id('sysLbl').textContent='OFFLINE'}
 const d=$id('sysdot');if(d)d.className='dot off'
 /* bridge down: park the execution buttons grey until data flows again
    (ids only exist on the panel page - skip elsewhere) */
 for(const n of ['1','2']){
  for(const bid of ['sellbtn'+n,'buybtn'+n]){
   const b=$id(bid);if(b){b.classList.add('off');b.disabled=true}}
  const spx=$id('sellpx'+n),apx=$id('buypx'+n);
  if(spx)spx.textContent='-';if(apx)apx.textContent='-'}}
function liveLink(sys){
 /* a terminal whose feed runs but which reports MT5's LOGGED-OUT marker
    (login 0) is NOT healthy: it must degrade the pill and show LOGOUT,
    not ride on the feed's 'LIVE' (login 0 once showed LIVE 13 ms while
    the balances read 0.00 and nothing could trade) */
 const t1=sys.t1||{},t2=sys.t2||{};
 const logout1=t1.logout,logout2=t2.logout;
 const ok=(t1.running&&(t1.logged_in!==false))&&(t2.running&&(t2.logged_in!==false));
 FAILS=0;                                 // a good response resets the streak
 const pill=$id('sysPill');
 if(pill){pill.className='status-pill'+(ok?'':' bad');
  $id('sysLbl').textContent=ok?'':
   ((logout1||logout2)?'LOGIN DROPPED':'LINK DEGRADED')}
 const d=$id('sysdot');if(d)d.className=ok?'dot':'dot off';
 return ok}
/* searchable <select>: hide options that do not contain the query
   (case-insensitive).  Used by the manual-execution, ORDER-ticket and
   schedule pair pickers so a 90-symbol broker feed stays usable. */
function filterSelect(selId,q){
  const sel=$id(selId);if(!sel)return;
  q=String(q==null?'':q).trim().toUpperCase();
  let visible=0,first=null;
  for(const o of sel.options){
    const hit=!q||String(o.text||o.value||'').toUpperCase().indexOf(q)>=0;
    o.hidden=!hit;
    if(hit){visible++;if(!first)first=o}}
  if(visible===1&&first){sel.value=first.value||first.text}}
/* suggestion drop-down under the *.symsearch inputs: the pair <select>
   holds the whole broker feed, so one generic helper serves the
   manual-execution, ORDER-ticket and schedule pickers alike.  The search
   box just filters the select; the .symlist paints the tappable hits. */
const SUGG={};
function symSearch(selId,boxId,inputId){
  const inp=$id(inputId);if(!inp)return;
  filterSelect(selId,inp.value);
  symSuggest(selId,boxId,inputId)}
function symSuggest(selId,boxId,inputId){
  const sel=$id(selId),box=$id(boxId),inp=$id(inputId);if(!sel)return;
  const q=String((inp&&inp.value)||'').trim().toUpperCase();
  const hits=[];
  for(const o of sel.options){
    const t=String(o.text||o.value||'');
    if(!q||t.toUpperCase().indexOf(q)>=0)hits.push(t);
    if(hits.length>=14)break}
  SUGG[selId]=hits;
  if(!box)return;
  if(!hits.length){box.classList.remove('open');box.innerHTML='';return}
  box.innerHTML=hits.map((s,i)=>'<div class="opt'+(s===sel.value?' on':'')+
   '" onclick="symPick(\\''+selId+'\\',\\''+boxId+'\\',\\''+inputId+'\\','+i+')">'+
   '<span class="mono">'+esc(s)+'</span><span class="pr">tap to arm</span></div>').join('');
  box.classList.add('open')}
function symPick(selId,boxId,inputId,i){
  const s=(SUGG[selId]||[])[i];if(!s)return;
  const sel=$id(selId);if(sel)sel.value=s;
  const inp=$id(inputId);if(inp)inp.value=s;
  const box=$id(boxId);if(box){box.classList.remove('open');box.innerHTML=''}}
/* ring gauge: pct 0..100, returns svg */
function ring(pct,col){
 pct=Math.max(0,Math.min(100,pct||0));
 const R=30,C=2*Math.PI*R,dash=C*pct/100;
 return '<div class="ring"><svg width="74" height="74" viewBox="0 0 74 74">'+
  '<circle cx="37" cy="37" r="'+R+'" fill="none" stroke="#16202e" stroke-width="7"/>'+
  '<circle cx="37" cy="37" r="'+R+'" fill="none" stroke="'+(col||'#4da6ff')+
  '" stroke-width="7" stroke-linecap="round" stroke-dasharray="'+dash.toFixed(1)+' '+C.toFixed(1)+'"/>'+
  '</svg><div class="pct">'+pct.toFixed(0)+'%</div></div>'}
/* ---------- rotating dial time picker ----------
   H / M / S each render as a circle: drag around it (mouse or touch) and the
   value follows the angle - 12 o'clock = 0, clockwise to max (24/60/60).
   Also responds to wheel steps. */
const DIALS={};   // key -> {val, steps, el}
function dialSvg(steps){
 let ticks='';
 const majEvery=(steps===24)?6:5;
 for(let i=0;i<steps;i++){
  const a=i*2*Math.PI/steps,maj=(i%majEvery===0);
  const r1=maj?24:28,r2=32;
  ticks+='<line class="tick'+(maj?' maj':'')+'" x1="'+(42+r1*Math.sin(a)).toFixed(2)+
   '" y1="'+(42-r1*Math.cos(a)).toFixed(2)+'" x2="'+(42+r2*Math.sin(a)).toFixed(2)+
   '" y2="'+(42-r2*Math.cos(a)).toFixed(2)+'"/>'}
 return '<svg viewBox="0 0 84 84"><circle class="track" cx="42" cy="42" r="38"/>'+
  ticks+'<circle class="prog" cx="42" cy="42" r="38"/>'+
  '<line class="arm" x1="42" y1="42" x2="42" y2="10"/>'+
  '<circle class="knob" cx="42" cy="4" r="4.5"/></svg>'}
function dialRender(k){
 const d=DIALS[k];if(!d)return;
 const frac=d.val/d.steps;
 const a=frac*2*Math.PI;                     // 12 o'clock = 0, clockwise
 const px=42+38*Math.sin(a),py=42-38*Math.cos(a);
 const svg=d.el.querySelector('svg');
 svg.querySelector('.prog').setAttribute('stroke-dasharray',
  (frac*2*Math.PI*38).toFixed(2)+' '+(2*Math.PI*38).toFixed(2));
 const arm=svg.querySelector('.arm');
 arm.setAttribute('x2',px.toFixed(2));arm.setAttribute('y2',py.toFixed(2));
 const knob=svg.querySelector('.knob');
 knob.setAttribute('cx',px.toFixed(2));knob.setAttribute('cy',py.toFixed(2));
 d.el.querySelector('.dial-val').textContent=String(d.val).padStart(2,'0')}
function dialSet(k,v){
 const d=DIALS[k];if(!d)return;
 d.val=((Math.round(v)%d.steps)+d.steps)%d.steps;
 dialRender(k);
 if(d.el._onchange)d.el._onchange(d.val)}
function dialFromEvent(e){
 const d=e.currentTarget._dial;if(!d)return;
 const svg=e.currentTarget.querySelector('svg');
 const r=svg.getBoundingClientRect();
 const x=e.clientX-(r.left+r.width/2),y=e.clientY-(r.top+r.height/2);
 let a=Math.atan2(x,-y);                  // 0 at 12 o'clock, clockwise
 if(a<0)a+=2*Math.PI;
 dialSet(d.key,a/(2*Math.PI)*d.steps)}
function dialWheel(e){
 const d=e.currentTarget._dial;if(!d)return;
 e.preventDefault();
 dialSet(d.key,d.val+(e.deltaY>0?-1:1))}
function makeDial(key,label,val,steps){
 const el=document.createElement('div');
 el.className='dial';el._dial={key:key,val:val,steps:steps};
 el.innerHTML=dialSvg(steps)+'<div class="dial-val">'+String(val).padStart(2,'0')+
  '</div><div class="dial-lbl">'+esc(label)+'</div>';
 DIALS[key]={val:val,steps:steps,el:el};
 let dragging=false;
 el.addEventListener('pointerdown',function(e){dragging=true;
  try{el.setPointerCapture(e.pointerId)}catch(err){}dialFromEvent(e)});
 el.addEventListener('pointermove',function(e){if(dragging)dialFromEvent(e)});
 el.addEventListener('pointerup',function(){dragging=false});
 el.addEventListener('pointercancel',function(){dragging=false});
 el.addEventListener('wheel',dialWheel,{passive:false});
 dialRender(key);
 return el}
function dialGet(key){const d=DIALS[key];return d?d.val:0}
function dialDestroyAll(){for(const k in DIALS)delete DIALS[k]}
/* AM/PM toggle beside an hour dial: the hour dial carries a 12-hour value
   (0-11) plus an AM/PM button.  '6:10' therefore means 06:10 in the MORNING
   only when AM is shown - clicking PM makes it 18:10.  The full 24-hour
   dial made '6:10 PM' silently schedule 06:10 AM (observed: two schedules
   created at 18:09 fired the NEXT morning).  dialGet('H') returns the
   0-23 hour; dialGet('ap') returns 0 for AM / 1 for PM. */
function toggleAP(key){const d=DIALS[key];if(!d)return;
 d.ap=(d.ap===1)?0:1;dialRender(key);
 if(d.el._onchange)d.el._onchange(d.val)}
function hour24(key){const d=DIALS[key];if(!d)return 0;
 return (d.val%12)+(d.ap?12:0)}
function apLabel(key){const d=DIALS[key];return d&&d.ap?'PM':'AM'}
/* live countdown under the open dials: 'fires in 2 m 10 s (18:10:00)' */
let AP_TIMER=null;
function apCountdown(n){const el=$id('cnt'+n);if(!el)return;
 const now=new Date();
 const t=new Date(now);t.setHours(hour24('eH'+n),dialGet('eM'+n),dialGet('eS'+n),0);
 if(t<=now)t.setTime(t.getTime()+86400000);
 const ds=Math.round((t-now)/1000);
 const hh=Math.floor(ds/3600),mm=Math.floor(ds%3600/60),ss=ds%60;
 const txt=(hh?hh+' h ':'')+(mm?mm+' m ':'')+ss+' s';
 el.textContent='fires in '+txt+' at '+t.toLocaleTimeString('en-GB')+' (machine clock)'}
/* ---------- shared schedule modal (panel + scheduled pages) ----------
   Open/close times are chosen on rotating dials; values are read back with
   dialGet()/hour24().  Defaults: open = machine now + 2 min, close = open
   + 5 min - in the SAME clock the operator sees in the top bar. */
function schedModalHTML(n,edit){
 edit=edit||null;
 const d=new Date();
 let s=d.getHours()*3600+d.getMinutes()*60+d.getSeconds()+120;
 const eh=Math.floor(s/3600)%24,em=Math.floor(s/60)%60,es=s%60;
 s+=300;                                   // close = open + 5 min
 const ch=Math.floor(s/3600)%24,cm=Math.floor(s/60)%60,cs=s%60;
 const e=edit||{};
 return '<div class="modal-head"><h3>'+(edit?'Adjust schedule #'+edit.id:'Schedule future trades')+' · account '+n+'</h3>'+
  '<button class="closex" onclick="closeModal()">×</button></div>'+
  '<div class="frow">'+
  '<div class="fgroup" style="grid-column:span 2"><label>Pair · search the broker feed</label>'+
   '<div class="symsearch"><input type="text" id="sps'+n+'" placeholder="type to filter - e.g. XAU, EUR..." autocomplete="off" oninput="symSearch(\\'sp'+n+'\\',\\'sphits'+n+'\\',\\'sps'+n+'\\')"></div>'+
   '<div class="symlist" id="sphits'+n+'"></div>'+
   '<select id="sp'+n+'">'+
   (edit?'<option>'+esc(edit.pair)+'</option>':'')+'</select></div>'+
  '<div class="fgroup"><label>Side</label><select id="sdf'+n+'">'+
   '<option value="BUY"'+(e.side!=='SELL'?' selected':'')+'>BUY</option><option value="SELL"'+(e.side==='SELL'?' selected':'')+'>SELL</option></select></div>'+
  '<div class="fgroup"><label>Lot size</label><input type="number" id="lotf'+n+'" step="0.01" min="0.01" value="'+(e.lot||0.01)+'"></div>'+
  '<div class="fgroup"><label>Positions</label><input type="number" id="npf'+n+'" step="1" min="1" max="50" value="'+(e.n||1)+'"></div>'+
  '</div>'+
  (edit?'':'<div class="secthint">quick arm · <b>fires in seconds</b> - one click sets open +30 s and close +90 s</div>'+
  '<div class="chiprow" style="margin:0 0 10px" id="quick'+n+'">'+
   [5,10,30,60,300].map(sec=>'<button type="button" class="lchip" onclick="quickArm('+n+','+sec+')">+'+
    (sec>=60?(sec/60)+' m':sec+' s')+'</button>').join('')+
  '</div>')+
  '<div class="secthint">open time · <b>drag the circles</b> · machine clock</div>'+
  '<div class="dials" id="dials_e'+n+'"></div>'+
  '<div class="cnt" id="cnt'+n+'"></div>'+
  '<div class="secthint">close time · auto-closes the positions</div>'+
  '<div class="dials" id="dials_c'+n+'"></div>'+
  '<div style="margin-top:16px;display:flex;gap:10px;align-items:center">'+
  '<button class="btn blue" id="schedSaveBtn" onclick="'+(edit?'saveScheduleEdit('+edit.id+','+n+')':'saveSchedule('+n+')')+'">'+(edit?'SAVE CHANGES':'SAVE SCHEDULE')+'</button>'+
  '<span class="feedback" style="margin:0" id="ffb'+n+'">fires ONCE at open time, auto-closes at close time · machine clock (AM/PM)</span>'+
  '</div>'+
  '<input type="hidden" id="dialinit'+n+'" value="'+(e.h!==undefined?e.h:eh)+'|'+(e.m!==undefined?e.m:em)+'|'+(e.s!==undefined?e.s:es)+'|'+(e.ch!==undefined?e.ch:ch)+'|'+(e.cm!==undefined?e.cm:cm)+'|'+(e.cs!==undefined?e.cs:cs)+'">'}
/* quick arm: open = now + sec, close = now + sec + 60 (or +5 m for the 5 m chip) */
function quickArm(n,sec){
 const cl=sec>=60?sec+300:sec+60;
 const t=new Date(Date.now()+sec*1000),c=new Date(Date.now()+cl*1000);
 DIALS['eH'+n].val=t.getHours()%12;DIALS['eH'+n].ap=t.getHours()>=12?1:0;
 DIALS['eM'+n].val=t.getMinutes();DIALS['eS'+n].val=t.getSeconds();
 DIALS['cH'+n].val=c.getHours()%12;DIALS['cH'+n].ap=c.getHours()>=12?1:0;
 DIALS['cM'+n].val=c.getMinutes();DIALS['cS'+n].val=c.getSeconds();
 for(const k in DIALS)if(DIALS[k].el)DIALS[k].el._onchange&&0;
 dialRender('eH'+n);dialRender('eM'+n);dialRender('eS'+n);
 dialRender('cH'+n);dialRender('cM'+n);dialRender('cS'+n);
 $id('ap_e'+n).textContent=apLabel('eH'+n);$id('ap_c'+n).textContent=apLabel('cH'+n);
 apCountdown(n);
 toast('armed: fires '+Math.round((t-new Date())/1000)+' s from now',true)}
function makeAPHour(n,prefix,val){
 const wrap=document.createElement('div');wrap.style.display='inline-block';
 const h12=val%12;const ap=val>=12?1:0;
 wrap.appendChild(makeDial(prefix+'H'+n,'hrs',h12,12));
 const btn=document.createElement('button');btn.type='button';
 btn.className='btn mini';btn.id='ap_'+prefix+n;
 btn.textContent=ap?'PM':'AM';
 btn.onclick=function(){toggleAP(prefix+'H'+n);btn.textContent=apLabel(prefix+'H'+n);
  if(AP_TIMER)apCountdown(n)};
 wrap.appendChild(btn);
 DIALS[prefix+'H'+n].ap=ap;
 return wrap}
function openSchedModal(n,edit){
 dialDestroyAll();
 if(AP_TIMER){clearInterval(AP_TIMER);AP_TIMER=null}
 SCHED_SAVING=false;                       // fresh modal = fresh save
 MODAL_ACC=n;
 $id('modalBody').innerHTML=schedModalHTML(n,edit||null);
 const parts=$id('dialinit'+n).value.split('|').map(Number);
 const de=$id('dials_e'+n),dc=$id('dials_c'+n);
 de.appendChild(makeAPHour(n,'e',parts[0]));
 de.appendChild(makeDial('eM'+n,'min',parts[1],60));
 de.appendChild(makeDial('eS'+n,'sec',parts[2],60));
 dc.appendChild(makeAPHour(n,'c',parts[3]));
 dc.appendChild(makeDial('cM'+n,'min',parts[4],60));
 dc.appendChild(makeDial('cS'+n,'sec',parts[5],60));
 apCountdown(n);
 AP_TIMER=setInterval(function(){apCountdown(n)},1000);
 fillSymbols();
 $id('modalBg').classList.add('open')}
/* ONE save at a time: a double-click on SAVE (or Enter-then-click) used to
   fire two POSTs back to back and create two identical schedules.  The
   guard also keeps the SAVE button disabled until the request settles. */
let SCHED_SAVING=false;
function setSaveBusy(busy){const b=$id('schedSaveBtn');
 if(b){b.disabled=busy;b.style.opacity=busy?'0.6':''}}
async function saveSchedule(n){if(SCHED_SAVING)return;SCHED_SAVING=true;
 const f=$id('ffb'+n);f.className='feedback';f.textContent='saving...';setSaveBusy(true);
 try{const j=await post('/api/schedule',{account:+n,pair:$id('sp'+n).value,
  side:$id('sdf'+n).value,
  lot:parseFloat($id('lotf'+n).value),n:+$id('npf'+n).value,
  exec:[hour24('eH'+n),dialGet('eM'+n),dialGet('eS'+n)],
  close:[hour24('cH'+n),dialGet('cM'+n),dialGet('cS'+n)]});
  const ft=new Date(j.next_fire);
  const today=ft.toDateString()===new Date().toDateString();
  const when=(j.next_fire_local||j.next_fire)+(today?'':' TOMORROW');
  f.className='feedback ok';f.textContent='saved #'+j.id+' - fires '+when;
  toast('schedule #'+j.id+' saved - fires '+when,today);
  // release the guard BEFORE the 1.5 s close delay: a user clicking SAVE
  // again in that window must get the server's 409 'already exists' (and
  // the button enabled), not a silently swallowed click (old code reset
  // the flag only in catch, so the second click did literally nothing).
  SCHED_SAVING=false;setSaveBusy(false);
  setTimeout(closeModal,1500);refresh()}catch(e){SCHED_SAVING=false;setSaveBusy(false);
  f.className='feedback err';f.textContent=e.message}}
async function saveScheduleEdit(sid,n){if(SCHED_SAVING)return;SCHED_SAVING=true;
 const f=$id('ffb'+n);f.className='feedback';f.textContent='saving...';setSaveBusy(true);
 try{const j=await post('/api/schedule/update',{id:sid,pair:$id('sp'+n).value,
  side:$id('sdf'+n).value,
  lot:parseFloat($id('lotf'+n).value),n:+$id('npf'+n).value,
  exec:[hour24('eH'+n),dialGet('eM'+n),dialGet('eS'+n)],
  close:[hour24('cH'+n),dialGet('cM'+n),dialGet('cS'+n)]});
  f.className='feedback ok';f.textContent='updated #'+sid+' - next fire '+(j.next_fire_local||j.next_fire);
  toast('schedule #'+sid+' adjusted - next fire '+(j.next_fire_local||j.next_fire),true);
  SCHED_SAVING=false;setSaveBusy(false);   // see saveSchedule
  setTimeout(closeModal,1200);refresh()}catch(e){SCHED_SAVING=false;setSaveBusy(false);
  f.className='feedback err';f.textContent=e.message}}
"""

DASHBOARD_TMPL = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>MT5 TERMINAL - DASHBOARD</title>
<style>{{ css|safe }}</style></head>
<body>
<div class="layout">
  <aside class="sidebar">
    <div class="sb-brand"><div class="logo"></div>MT5<b>//</b>TERM</div>
    <div class="sb-sub">trading terminal</div>
    <div class="sb-section">Workspace</div>
    <a class="sb-item on" href="/"><span class="ic">◈</span>Dashboard</a>
    <a class="sb-item" href="/panel"><span class="ic">▣</span>Trading Panel</a>
    <a class="sb-item" href="/scheduled"><span class="ic">◇</span>Scheduled Trades</a>
    <a class="sb-item" href="/factory"><span class="ic">◈</span>Factory</a>
    <div class="sb-foot">SPOTDUMP EA · v2</div>
  </aside>
  <main class="main">
    <header class="topbar">
      <div>
        <div class="page-title">DASHBOARD</div>
        <div class="page-sub">live account metrics · 1 s refresh</div>
      </div>
      <div class="spacer"></div>
      <div class="status-pill" id="sysPill"><span class="dot" id="sysdot"></span>
        <span id="sysLbl"></span></div>
      <div id="clock" class="mono"></div>
    </header>
    <div class="wrap">
      <section class="kpi-grid" id="kpiGrid"></section>
      <div class="grid accounts">
        <section class="panel acc" id="acc1"></section>
        <section class="panel acc" id="acc2"></section>
      </div>
    </div>
  </main>
</div>
<div class="toast" id="toast"></div>
<script>{{ js|safe }}</script>
</body></html>"""

DASHBOARD_JS = COMMON_JS + """
let S=null;
function renderKpis(S){
 const sys=S.system||{};
 const a1=S.accounts['1']||{},a2=S.accounts['2']||{};
 const eqTotal=(a1.equity||0)+(a2.equity||0);
 const plTotal=(a1.profit||0)+(a2.profit||0);
 const open=((a1.positions||0)+((a2.positions)||0));
 const t1=sys.t1||{},t2=sys.t2||{};
 const live=(t1.running&&t2.running&&(a1.live!==false)&&(a2.live!==false));
 const cards=[
  {k:'TOTAL EQUITY',v:fmt(eqTotal),c:'',sub:'both accounts'},
  {k:'FLOATING P/L',v:signed(plTotal),c:cls(plTotal),sub:'unrealized'},
  {k:'OPEN POSITIONS',v:open,c:'',sub:'across both terminals'},
  {k:'SCHEDULED',v:sys.schedules_active||0,c:'',sub:'future trades armed'},
  {k:'EXECUTOR',v:sys.scheduler?'ON':'OFF',c:sys.scheduler?'up':'down',sub:'trade bus'},
  {k:'METRICS',v:sys.metrics?'ON':'OFF',c:sys.metrics?'up':'down',sub:'observer'},
  {k:'T1 FEED',v:t1.running?fmt(t1.age,1)+'s':'DOWN',c:t1.running?'up':'down',sub:'terminal 1'},
  {k:'T2 FEED',v:t2.running?fmt(t2.age,1)+'s':'DOWN',c:t2.running?'up':'down',sub:'terminal 2'},
 ];
 $id('kpiGrid').innerHTML=cards.map(c=>
  '<div class="kpi"><div class="k">'+c.k+'</div>'+
  '<div class="v '+(c.c||'')+'">'+c.v+'</div>'+
  '<div class="sub">'+(c.sub||'')+'</div></div>').join('');
}
function sysBody(s){
 const row=(n)=>{const a=s.accounts[n]||{};const live=a.live;
  return '<tr><td>ACC '+n+'</td><td>'+(a.login||'-')+'</td>'+
   '<td class="num">'+fmt(a.balance)+'</td><td class="num">'+fmt(a.equity)+'</td>'+
   '<td class="num '+cls(a.profit)+'">'+signed(a.profit)+'</td>'+
   '<td>'+(live?'<span class="up">LIVE</span>':'<span class="down">STALE '+fmt(a.age_s,0)+'s</span>')+'</td></tr>'};
 return '<div class="tblwrap"><table><tr><th>acc</th><th>login</th><th class="num">balance</th>'+
  '<th class="num">equity</th><th class="num">float</th><th>feed</th></tr>'+
  row('1')+row('2')+'</table></div>'}
function card(n){const a=S.accounts[n]||{};const st=a.stats||{};
 const wr=st.winrate||0;
 const ringCol=wr>=50?'#4da6ff':(wr>=35?'#eab308':'#ff5c5c');
 if(!a.live&&!a.login){
  return '<div class="acc-head"><span class="dot off"></span>'+
   '<span class="name">ACCOUNT '+n+'</span>'+
   '<span class="sub">terminal offline</span></div>'+
   '<div class="empty">waiting for the MT5 bridge...<br><span class="mut">'+
   'start bridge.py, then this card fills in live</span></div>'}
 const out0=String(a.login||'')===''||String(a.login||'')==='0';
 return '<div class="acc-head">'+
  '<span class="'+dot(a.live&&!out0)+'"></span>'+
  '<span class="name">ACCOUNT '+n+'</span>'+
  (out0?'<span class="sub" style="color:#ff5c5c;font-weight:600">NOT LOGGED IN</span>'
       :'<span class="sub mono">'+esc(a.login||'-')+'</span>')+
  '<span class="sub">'+esc(a.server||'-')+'</span>'+
  '<span class="sub">'+esc(a.broker||'-')+'</span>'+
  '<span class="sub">'+esc(a.trade_mode||'').toUpperCase()+'</span>'+
   '<span class="spacer"></span><span class="sub mono">'+esc(a.currency||'')+'</span></div>'+
  (a.syncing?'<div class="sect"><h4 style="color:var(--yellow)">Broker sync pending - balances fill in shortly</h4></div>':'')+
  '<div style="padding:13px 13px 0"><div class="kv">'+
  kv('Balance',fmt(a.balance),'')+kv('Equity',fmt(a.equity),cls(a.equity-a.balance))+
  kv('Floating P/L',signed(a.profit),cls(a.profit))+
  kv('Margin level',a.margin_level>0?fmt(a.margin_level,1)+'%':'-','')+
 '</div></div>'+
 '<div class="sect"><h4>Performance</h4></div>'+
 '<div style="padding:13px">'+
  '<div style="display:flex;gap:18px;align-items:center;flex-wrap:wrap">'+
   ring(wr,ringCol)+
   '<div style="flex:1;min-width:220px"><div class="statgrid">'+
    stat('Trades',st.trades_taken,0)+stat('Wins',st.wins,0)+stat('Losses',st.losses,0)+
    stat('Open',st.open,0)+stat('Net closed',st.net_closed,2,cls(st.net_closed))+
   '</div></div>'+
  '</div>'+
  '<div class="legend"><span><i style="background:var(--blue)"></i>WINS '+st.wins+'</span>'+
  '<span><i style="background:var(--red)"></i>LOSSES '+st.losses+'</span></div>'+
 '</div>'+
 '<div class="sect"><h4>Equity curve</h4><div class="right mut" style="font-size:10px">'+
  fmt((a.equity_curve||[]).length,0)+' samples · 10s</div></div>'+
 '<div style="padding:13px"><div class="chartbox" id="chart'+n+'">'+
  chart(a.equity_curve)+'</div></div>'+
 '<div class="sect"><h4>Traded pairs</h4></div><div style="padding:9px 13px 13px">'+
  pairs(a.pairs)+'</div>'}
function kv(k,v,c){return '<div><div class="k">'+k+'</div><div class="v '+c+'">'+v+'</div></div>'}
function stat(k,v,d,c){return '<div><div class="k">'+k+'</div>'+
 '<div class="v sm '+(c||'')+'">'+fmt(v,d)+'</div></div>'}
function chart(curve){if(!curve||curve.length<2)
  return '<div class="empty" style="line-height:190px">collecting samples...</div>';
 const w=1000,h=190,pad=8;
 let min=1e18,max=-1e18;
 for(const p of curve){min=Math.min(min,p[1],p[2]);max=Math.max(max,p[1],p[2])}
 if(max-min<1e-9){max+=1;min-=1}
 const X=i=>pad+(w-2*pad)*i/(curve.length-1);
 const Y=v=>pad+(h-2*pad)*(1-(v-min)/(max-min));
 const line=(idx)=>curve.map((p,i)=>X(i).toFixed(1)+','+Y(p[idx]).toFixed(1)).join(' ');
 const grid=(y)=>'<line x1="0" y1="'+y+'" x2="'+w+'" y2="'+y+
  '" stroke="#16202e" stroke-width="1"/>';
 return '<svg viewBox="0 0 '+w+' '+h+'" preserveAspectRatio="none">'+
  grid(Y(max))+grid(Y((max+min)/2))+grid(Y(min))+
  '<polyline points="'+line(1)+'" fill="none" stroke="#7d8ba0" stroke-width="1" stroke-dasharray="3 3" opacity=".5"/>'+
  '<polyline points="'+line(2)+'" fill="none" stroke="#4da6ff" stroke-width="2" '+
  'style="filter:drop-shadow(0 0 4px rgba(77,166,255,.6))"/>'+
  '</svg>'+
  '<div class="legend"><span><i style="background:#7d8ba0;opacity:.5"></i>BALANCE '+fmt(curve[curve.length-1][1])+'</span>'+
  '<span><i style="background:#4da6ff"></i>EQUITY '+fmt(curve[curve.length-1][2])+'</span>'+
  '<span class="mut">min '+fmt(min)+'</span><span class="mut">max '+fmt(max)+'</span></div>'}
function pairs(p){p=p||{};const keys=Object.keys(p);
 if(!keys.length)return '<div class="empty">no trades recorded yet</div>';
 const max=Math.max.apply(null,keys.map(k=>p[k]));
 return keys.map(k=>'<div class="pairrow"><span class="mono">'+esc(k)+'</span>'+
  '<div class="bar"><i style="width:'+(100*p[k]/max).toFixed(1)+'%"></i></div>'+
  '<span class="num mono mut" style="text-align:right">'+p[k]+'</span></div>').join('')}
function render(){if(!S)return;
  for(const n of ['1','2'])$id('acc'+n).innerHTML=card(n);
  renderKpis(S);
  const sys=S.system||{};liveLink(sys);}
async function refresh(){try{S=await api('/api/dashboard');render()}
 catch(e){linkOffline();
  // first load failed: show why - later failures keep the last good render
  const kp=$id('kpiGrid');
  if(kp&&kp.children.length===0)
   kp.innerHTML='<div class="err">dashboard offline - '+esc(e.message)+
    ' - retrying every second</div>'}}
refresh();setInterval(refresh,1000);
"""

def render_dashboard() -> str:
    return render_template_string(DASHBOARD_TMPL, css=CSS, js=DASHBOARD_JS)

PANEL_TMPL = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>MT5 TERMINAL - PANEL</title>
<style>{{ css|safe }}</style></head>
<body>
<div class="layout">
  <aside class="sidebar">
    <div class="sb-brand"><div class="logo"></div>MT5<b>//</b>TERM</div>
    <div class="sb-sub">trading terminal</div>
    <div class="sb-section">Workspace</div>
    <a class="sb-item" href="/"><span class="ic">◈</span>Dashboard</a>
    <a class="sb-item on" href="/panel"><span class="ic">▣</span>Trading Panel</a>
    <a class="sb-item" href="/scheduled"><span class="ic">◇</span>Scheduled Trades</a>
    <a class="sb-item" href="/factory"><span class="ic">◈</span>Factory</a>
    <div class="sb-foot">SPOTDUMP EA · v2</div>
  </aside>
  <main class="main">
    <header class="topbar">
      <div>
        <div class="page-title">TRADING PANEL</div>
        <div class="page-sub">one-click execution · 0.6 s refresh</div>
      </div>
      <div class="spacer"></div>
      <div class="status-pill" id="sysPill"><span class="dot" id="sysdot"></span>
        <span id="sysLbl"></span></div>
      <div id="clock" class="mono"></div>
    </header>
    <div class="wrap">
      <div class="closebar"><button class="btn close-main" id="closeAllTopBtn"
        onclick="closeAllBoth(this)"
        title="close every position AND delete every stop/limit order (incl. StopExe straddles) on ALL accounts at once">✕ CLOSE</button></div>
      <div class="grid accounts">
        <section class="panel acc" id="acc1"></section>
        <section class="panel acc" id="acc2"></section>
      </div>
      <section class="panel" id="engineCard">
        <div class="sect"><h4>Account Integration</h4>
          <div class="right">
            <span class="chip" id="engBusy">IDLE</span>
            <button class="btn mini" onclick="engRefresh()">REFRESH</button>
          </div></div>
        <div class="tblwrap" id="engSlots"><div class="empty">loading...</div></div>
        <div class="sect"><h4>Saved accounts</h4>
          <div class="right"><button class="btn mini blue" onclick="engToggleSave()">+ SAVE ACCOUNT</button></div></div>
        <div class="tblwrap" id="engSaved"><div class="empty">loading...</div></div>
        <div id="engSaveForm" style="display:none;margin-top:4px;
          border-top:1px solid color-mix(in srgb,currentColor 12%,transparent);
          padding-top:10px">
          <div class="frow" style="margin-top:8px">
            <div class="fgroup" style="grid-column:span 2"><label>Account number</label>
              <input id="engSaveLoginIn" type="text" inputmode="numeric" autocomplete="off" placeholder="e.g. 51234567"></div>
            <div class="fgroup" style="grid-column:span 2"><label>Server</label>
              <input id="engSaveServerIn" type="text" autocomplete="off" placeholder="FxPro-MT5 / HFM-Real"></div>
            <div class="fgroup" style="grid-column:span 2"><label>Password</label>
              <input id="engSavePassIn" type="password" autocomplete="new-password" placeholder="saved password"></div>
            <div class="fgroup" style="grid-column:span 2"><label>Label (optional)</label>
              <input id="engSaveLabelIn" type="text" autocomplete="off" placeholder="e.g. FxPro live"></div>
          </div>
          <div class="feedback" id="engSaveFeedback"></div>
          <div class="frow" style="margin-top:6px">
            <button class="btn blue" style="grid-column:span 2" onclick="engSaveSubmit()">SAVE</button>
            <button class="btn" style="grid-column:span 2" onclick="engToggleSave(true)">CANCEL</button>
          </div>
          <div class="body mut" style="font-size:11px;padding-top:6px">
            saving never touches a terminal - it just stores the info so the
            account can be logged in later with one click (T1 / T2).</div>
        </div>
        <div id="engForm" style="display:none;margin-top:12px;
          border-top:1px solid color-mix(in srgb,currentColor 12%,transparent);
          padding-top:10px">
          <div class="sect"><h4>Log in - Terminal <span id="engFormT">1</span></h4>
            <div class="right"><button class="btn mini" onclick="engCloseForm()">CANCEL</button></div></div>
          <div class="frow" style="margin-top:8px">
            <div class="fgroup" style="grid-column:span 2"><label>Saved account</label>
              <select id="engKnownIn" onchange="engPickKnown()"><option value="">- pick a saved account -</option></select></div>
            <div class="fgroup" style="grid-column:span 2"><label>Account number</label>
              <input id="engLoginIn" type="text" inputmode="numeric" autocomplete="off" placeholder="e.g. 51234567"></div>
            <div class="fgroup" style="grid-column:span 2"><label>Server</label>
              <input id="engServerIn" type="text" autocomplete="off" placeholder="HFM-Real / MetaQuotes-Demo"></div>
            <div class="fgroup" style="grid-column:span 4"><label>Password</label>
              <input id="engPassIn" type="password" autocomplete="current-password" placeholder="leave blank to use a saved password"></div>
          </div>
          <div class="feedback" id="engFeedback"></div>
          <div class="frow" style="margin-top:6px">
            <button class="btn blue" style="grid-column:span 2" onclick="engSubmitLogin()">LOG IN</button>
          </div>
        </div>
        <div id="engApForm" style="display:none;margin-top:12px;
          border-top:1px solid color-mix(in srgb,currentColor 12%,transparent);
          padding-top:10px">
          <div class="sect"><h4>Access point for <span id="engApServer">-</span></h4>
            <div class="right"><button class="btn mini" onclick="engApClose()">CANCEL</button></div></div>
          <div class="frow" style="margin-top:8px">
            <div class="fgroup" style="grid-column:span 4"><label>host:port</label>
              <input id="engApAddrIn" type="text" autocomplete="off" placeholder="e.g. mt5.cxmdirect.com:443"></div>
          </div>
          <div class="feedback" id="engApFeedback"></div>
          <div class="frow" style="margin-top:6px">
            <button class="btn blue" style="grid-column:span 2" onclick="engApSubmit()">SAVE</button>
          </div>
          <div class="body mut" style="font-size:11px;padding-top:6px">
            MT5 only dials a trade server it already lists, so a server it has
            never seen needs its host:port once - after that the terminal
            learns it and plain name logins keep working.</div>
        </div>
        <div class="body mut" style="font-size:11px;padding-top:8px">
          any account (live or demo) into either terminal - TIP: already
          logged in inside MT5? just press ADOPT.</div>
      </section>
    </div>
  </main>
</div>
<div class="modal-bg" id="modalBg">
  <div class="modal" id="modalBody"></div>
</div>
<div class="news-bg" id="newsBg">
  <section class="news-panel" id="newsBody">
    <div class="news-head">
      <div class="title">NEWS TRADER</div>
      <div class="sub" id="newsSrc">calendar</div>
      <div style="flex:1"></div>
      <button class="news-closer" id="newsCloserBtn" aria-pressed="false"
        onclick="newsToggleCloser()"
        title="close every position on the account 5 s AFTER the release (10 s execution gap)">
        <span class="knob"></span><span>CLOSER <b id="newsCloserLbl">OFF</b></span></button>
      <button class="news-x" onclick="closeNews()" title="decline / close">✕</button>
    </div>
    <div class="news-bar">
      <button class="chip filter on" id="newsHighChip" onclick="newsToggleHigh()">
        HIGH IMPACT ONLY</button>
      <span class="chip">WEEK <b id="newsCount">0</b></span>
      <span class="chip">NOW <b id="newsNow">-</b></span>
      <span class="chip">WINDOW <b>T-5s → T+5s</b></span>
      <div class="spacer"></div>
      <span class="chip" id="newsPos">-</span>
    </div>
    <div class="news-slide">
      <button class="news-nav" id="newsPrev" onclick="newsSlide(-1)"
        title="earlier event">&lt;</button>
      <div class="news-card" id="newsCard">
        <div class="empty">loading the calendar...</div></div>
      <button class="news-nav" id="newsNext" onclick="newsSlide(1)"
        title="later event">&gt;</button>
    </div>
    <div class="news-controls" id="newsControls"></div>
    <div class="news-quote" id="newsQuote"></div>
    <div class="news-plans" id="newsPlans"></div>
    <div class="news-foot">
      <button class="tbtn news-decline" onclick="newsDecline()">DECLINE</button>
      <button class="tbtn news-trade" id="newsTradeBtn" onclick="newsTrade()">TRADE</button>
    </div>
  </section>
</div>
<div class="toast" id="toast"></div>
<script>{{ js|safe }}</script>
</body></html>"""

PANEL_JS = COMMON_JS + """
let P=null,SCH=null,BUILT=false,MODAL_ACC=null,INFLIGHT={},ENG=null,ENGOPS=null;
function num(v){return (v===null||v===undefined||isNaN(+v))?0:+v}
function setBusy(n,busy){for(const b of document.querySelectorAll('#acc'+n+' .tbtn'))
 b.disabled=busy||b.classList.contains('off')}
function hhmmss(h,m,s){return String(h).padStart(2,'0')+':'+String(m).padStart(2,'0')+':'+String(s).padStart(2,'0')}
/* Schedules are stored and fired in UTC.  The panel used to render bare
   H:M:S with no zone, so an operator in UTC+3 who scheduled 14:30 saw the
   order fire at 17:30 local.  CLOCK is fetched once and used to label the
   fields and show the local equivalent. */
let CLOCK=null;
fetch('/api/clock').then(r=>r.json()).then(j=>{if(j&&j.ok)CLOCK=j}).catch(()=>{});
function tzTag(){return '<span class="mut" style="font-weight:400">UTC</span>'}
function tzOff(){return CLOCK?CLOCK.utc_offset_minutes:-new Date().getTimezoneOffset()}
function tzHint(){const m=tzOff();if(!m)return 'times are UTC · fires ONCE at exec time, auto-closes at close time';
 const sg=m<0?'-':'+',a=Math.abs(m),hh=String(Math.floor(a/60)).padStart(2,'0'),mm=String(a%60).padStart(2,'0');
 return 'times are UTC - your clock is UTC'+sg+hh+':'+mm+', so 00:00 here = '+utcToLocal(0,0,0)+' local'}
function utcToLocal(h,m,s){const t=(h*3600+m*60+s+tzOff()*60+86400*2)%86400;
 return String(Math.floor(t/3600)).padStart(2,'0')+':'+String(Math.floor(t%3600/60)).padStart(2,'0')+
        ':'+String(t%60).padStart(2,'0')}
function optRange(a,b,sel){let o='';for(let i=a;i<=b;i++){o+='<option value="'+i+'"'+
 (i==sel?' selected':'')+'>'+String(i).padStart(2,'0')+'</option>'}return o}
function buildPanel(n){
 $id('acc'+n).innerHTML=
 '<div class="acc-head">'+
  '<span class="dot off" id="live'+n+'"></span>'+
  '<span class="name">ACCOUNT '+n+'</span>'+
  '<span class="sub mono" id="login'+n+'">-</span>'+
  '<span class="sub" id="server'+n+'">-</span>'+
  '<span class="sub" id="broker'+n+'">-</span>'+
  '<span class="sub"><button class="btn mini danger" onclick="restartTerm('+n+')">RESTART</button></span>'+
  '<span class="spacer"></span><span class="sub mono" id="cur'+n+'"></span>'+
  (String(n)==='1'?'':'<button class="btn close-top danger" id="closeTopBtn'+n+'" onclick="closeAll('+n+',this)"'+
  ' title="close every position on account '+n+'">✕ CLOSE</button>')+'</div>'+
 '<div style="padding:13px 13px 0"><div class="chips" id="spread'+n+'"></div></div>'+
 '<div style="padding:13px"><div class="kv">'+
  kv('Balance','id:bal'+n)+kv('Equity','id:eq'+n)+kv('Floating P/L','id:pl'+n)+
  kv('Margin','id:mg'+n)+kv('Free margin','id:mgf'+n)+kv('Margin level','id:mgl'+n)+
  kv('Leverage','id:lev'+n)+kv('Open positions','id:np'+n)+
 '</div></div>'+
 '<div class="sect"><h4>One click trading</h4>'+
  '<div class="right"><span class="chip">SPREAD <b id="tspr'+n+'">-</b></span></div></div>'+
 '<div class="body">'+
  '<div class="frow" style="margin-bottom:10px">'+
   '<div class="fgroup" style="grid-column:span 2"><label>Symbol · search the broker feed</label>'+
    '<div class="symsearch"><input type="text" id="symsearch'+n+'" placeholder="type to filter - e.g. XAU, EUR..." autocomplete="off" oninput="symSearch(\\'sym'+n+'\\',\\'symhits'+n+'\\',\\'symsearch'+n+'\\')"></div>'+
    '<div class="symlist" id="symhits'+n+'"></div>'+
    '<select id="sym'+n+'"></select></div>'+
   '<div class="fgroup"><label>Lot</label>'+
    '<div class="lotrow"><button type="button" onclick="stepLot('+n+',-0.01)">-</button>'+
    '<input id="lot'+n+'" type="number" step="0.01" min="0.01" value="0.01">'+
    '<button type="button" onclick="stepLot('+n+',0.01)">+</button></div></div>'+
   '<div class="fgroup"><label>Orders</label>'+
    '<div class="lotrow"><button type="button" onclick="stepOrd('+n+',-1)">-</button>'+
    '<input id="ordn'+n+'" type="number" step="1" min="1" max="50" value="1">'+
    '<button type="button" onclick="stepOrd('+n+',1)">+</button></div></div>'+
  '</div>'+
  '<div class="chiprow" id="lchips'+n+'"></div>'+
  '<div class="chiprow" id="ochips'+n+'"></div>'+
  '<div class="tradebtns" style="margin-top:12px">'+
   '<button class="tbtn tsell off" id="sellbtn'+n+'" disabled onclick="doTrade('+n+',\\'SELL\\')"><span class="lbl">SELL</span><span class="px" id="sellpx'+n+'">-</span></button>'+
   '<button class="tbtn tbuy off" id="buybtn'+n+'" disabled onclick="doTrade('+n+',\\'BUY\\')"><span class="lbl">BUY</span><span class="px" id="buypx'+n+'">-</span></button>'+
  '</div>'+
  '<div class="feedback" id="fb'+n+'">market execution via bridge</div>'+
  '<div class="order-wrap" style="gap:8px;flex-wrap:wrap">'+
   '<button class="btn order-mini" id="ordBtn'+n+'" '+
    'onclick="asmrPop(this);openOrderModal('+n+')">◌ ORDER</button>'+
   '<button class="btn order-mini" onclick="asmrPop(this);openSchedModal('+n+')">◇ SCHEDULE</button>'+
   '<button class="btn order-mini" onclick="asmrPop(this);stexJump('+n+')">STOPEXE ↓</button>'+
   '<button class="btn order-mini news" id="newsBtn'+n+'" onclick="asmrPop(this);openNews('+n+')"'+
   ' title="upcoming news - slide the week, arm a straddle 5 s before the release">◈ NEWS</button></div>'+
  '</div>'+
  '<div class="sect"><h4>StopExe · straddle stops</h4>'+
   '<div class="right"><span class="chip">BRACKET <b id="stexbr'+n+'">±0.5 pips</b></span></div></div>'+
  '<div class="body" id="stexbody'+n+'">'+
   '<div class="fgroup"><label>Search pair · broker feed</label>'+
    '<div class="symsearch"><input type="text" id="stexsearch'+n+'" placeholder="type XAU, EUR, GBP... (live quotes only)" autocomplete="off" oninput="stexFilter('+n+')"></div>'+
    '<div class="symlist" id="stexlist'+n+'"></div></div>'+
   '<div class="stex-live"><span class="chip">PAIR <b id="stexsym'+n+'">-</b></span>'+
    '<span class="chip">BID <b id="stexbid'+n+'">-</b></span>'+
    '<span class="chip">ASK <b id="stexask'+n+'">-</b></span>'+
    '<span class="chip">SPREAD <b id="stexspread'+n+'">-</b></span></div>'+
   '<div class="stex-bracket">'+     '<div><div class="k" id="stexbuyk'+n+'">Buy stop · ask + 0.5 pips</div><div class="v buy" id="stexbuy'+n+'">-</div>'+
     '<div class="mut" style="font-size:11px;margin-top:2px">SL <span class="mono" id="stexbuysl'+n+'">-</span> · <span id="stexbuyslp'+n+'">0.5</span> pips</div></div>'+
    '<div><div class="k" id="stexsellk'+n+'">Sell stop · bid - 0.5 pips</div><div class="v sell" id="stexsell'+n+'">-</div>'+
     '<div class="mut" style="font-size:11px;margin-top:2px">SL <span class="mono" id="stexsellsl'+n+'">-</span> · <span id="stexsellslp'+n+'">0.5</span> pips</div></div>'+
   '</div>'+
   '<div class="frow" style="margin-bottom:10px">'+
    '<div class="fgroup"><label>Lot (each leg)</label>'+
     '<div class="lotrow"><button type="button" onclick="stexStepLot('+n+',-0.01)">-</button>'+
     '<input id="stexlot'+n+'" type="number" step="0.01" min="0.01" value="0.01">'+
     '<button type="button" onclick="stexStepLot('+n+',0.01)">+</button></div></div>'+
    '<div class="fgroup"><label>Bracket</label>'+
'<select id="stexpips'+n+'" onchange="stexPreview('+n+',true)">'+
       '<option value="0.5" selected>±0.5 pips (1 total)</option>'+
       '<option value="1">±1 pip (2 total)</option>'+
       '<option value="2">±2 pips (4 total)</option>'+
       '<option value="5">±5 pips (10 total)</option></select></div>'+
    '<div class="fgroup"><label>SL spread cushion</label>'+
     '<input class="stex-spr" id="stexspr'+n+'" type="number" step="0.1" min="0" max="100" value="0" '+
      'onchange="stexPreview('+n+',true)" title="extra pips added past the live bid/ask spread to the SL floor"></div>'+
   '</div>'+
   '<div class="tradebtns" style="margin-top:2px">'+
    '<button class="tbtn stex" id="stexbtn'+n+'" onclick="doStopexe('+n+')">EXECUTE STOPEXE</button>'+
   '</div>'+
   '<div class="feedback" id="stexfb'+n+'">straddle: buy stop + sell stop around the live price · each leg carries a 0.5-pip SL, floored just beyond the live spread</div>'+
  '</div>'+
  '<div class="sect"><h4>Open positions</h4><div class="right">'+
   '<span class="chip">P/L <b id="pospl'+n+'">-</b></span></div></div>'+
   '<div class="tblwrap" id="pos'+n+'"><div class="empty">no open positions</div></div>'+
  '<div class="sect"><h4>Pending orders · stop / limit</h4><div class="right">'+
   '<span class="chip">ORDERS <b id="pendcnt'+n+'">0</b></span>'+
   '<button class="btn mini danger" onclick="closeAll('+n+',null)">✕ CLOSE ALL</button></div></div>'+
   '<div class="tblwrap" id="pend'+n+'"><div class="empty">no pending orders</div></div>'+
 '<div class="sect"><h4>Scheduled trades</h4><div class="right">'+
  '<span class="chip">ACTIVE <b id="schcnt'+n+'">0</b></span>'+
  '<button class="btn mini blue" onclick="openSchedModal('+n+')">+ SCHEDULE</button></div></div>'+
  '<div class="body mut" style="font-size:11px">see the <a href="/scheduled" '+
  'style="color:var(--blue)">Scheduled Trades</a> page for the list & fired log</div>'}
function kv(k,ref){return '<div><div class="k">'+k+'</div><div class="v sm" id="'+ref.split(':')[1]+'">-</div></div>'}
function fillSymbols(){const spots=(((P||{}).accounts||{})['1']||{}).spreads||{};
 const keys=Object.keys(spots);
 for(const n of ['1','2']){const sel=$id('sym'+n),sp=$id('sp'+n);
  for(const el of [sel,sp]){if(!el)continue;const cur=el.value;
   el.innerHTML=(cur&&!keys.includes(cur)?'<option>'+esc(cur)+'</option>':'')+
    keys.map(k=>'<option'+(k==cur?' selected':'')+'>'+k+'</option>').join('');
   if(cur)el.value=cur}
  /* keep an active manual-execution search applied across feed refreshes */
  const q=($id('symsearch'+n)||{}).value;
  if(q)filterSelect('sym'+n,q)}}
function stepLot(n,d){const el=$id('lot'+n);
 let v=Math.max(0.01,(parseFloat(el.value)||0.01)+d);el.value=v.toFixed(2);markChip(n)}
function setLot(n,v){$id('lot'+n).value=v.toFixed(2);markChip(n)}
function markChip(n){const v=(parseFloat($id('lot'+n).value)||0).toFixed(2);
 for(const c of document.querySelectorAll('#lchips'+n+' .lchip'))
  c.className='lchip'+(c.textContent==v?' on':'')}
function stepOrd(n,d){const el=$id('ordn'+n);if(!el)return;
 let v=Math.max(1,Math.min(50,(parseInt(el.value,10)||1)+d));el.value=v;markOrdChip(n)}
function setOrd(n,v){$id('ordn'+n).value=Math.max(1,Math.min(50,v|0||1));markOrdChip(n)}
function markOrdChip(n){const v=String(parseInt(($id('ordn'+n)||{}).value,10)||1);
 for(const c of document.querySelectorAll('#ochips'+n+' .lchip'))
  c.className='lchip'+(c.textContent==v?' on':'')}
function ordCount(n){const el=$id('ordn'+n);if(!el)return 1;
 const v=parseInt(el.value,10);return (v>=1&&v<=50)?v:1}
async function doTrade(n,side){
 if(INFLIGHT['t'+n])return;                    // one click = one order
 const symEl=$id('sym'+n),lotEl=$id('lot'+n),fb=$id('fb'+n);
 if(!symEl||!lotEl||!fb)return;                // panel not built yet
 const sym=(symEl.value||'').trim();   // case-sensitive MT5 symbol name
 const lot=parseFloat(lotEl.value);
 const cnt=ordCount(n);
 if(!sym){fb.className='feedback err';fb.textContent='no symbol - waiting for the feed';return}
 if(!(lot>0)){fb.className='feedback err';fb.textContent='lot must be greater than 0';return}
 INFLIGHT['t'+n]=true;setBusy(n,true);
 fb.className='feedback';fb.textContent=side+' '+sym+' '+lot+(cnt>1?' x'+cnt:'')+' ... sending';
 const t0=performance.now();
 try{const j=await post('/api/trade',{account:+n,symbol:sym,side:side,lot:lot,n:cnt});
  const ms=Math.round(performance.now()-t0);
  const got=(j.n||j.ok||1), tk=j.tickets||(j.ticket?[j.ticket]:[]);
  fb.className='feedback ok';
  fb.textContent='FILLED '+(got>1?got+'/'+cnt+' ':'')+j.detail+' @ '+j.price+' ('+ms+' ms)';
  toast(side+' '+sym+' '+lot+(cnt>1?' x'+cnt:'')+' filled @ '+j.price+
   (tk.length>1?' ('+tk.length+' tickets)':'')+' in '+ms+' ms',true);
  refresh()}
 catch(e){fb.className='feedback err';
  fb.textContent='REJECTED: '+e.message;toast(e.message,false)}
 finally{delete INFLIGHT['t'+n];setBusy(n,false)}}
async function closePos(n,ticket){
 if(INFLIGHT['c'+ticket])return;INFLIGHT['c'+ticket]=true;
 try{const j=await post('/api/close',{account:+n,ticket:String(ticket)});
  toast('closed #'+ticket+' - '+(j.detail||'ok'),true);refresh()}
 catch(e){toast(e.message,false)}
 finally{delete INFLIGHT['c'+ticket]}}
async function closeAll(n,el){
 if(INFLIGHT['ca'+n])return;INFLIGHT['ca'+n]=true;
 const b=el||$id('closeTopBtn'+n)||(String(n)==='1'?$id('closeMainBtn1'):null);
 if(b){b.disabled=true;if(b.classList)b.classList.remove('pop');}
 try{const j=await post('/api/close',{account:+n,symbol:'ALL'});
  toast(j.detail||'close all sent',j.ok);refresh()}
 catch(e){toast(e.message,false)}
 finally{delete INFLIGHT['ca'+n];if(b)b.disabled=false}}
/* the red CLOSE bar above the panels: closes everything on BOTH
   terminals at once via close.py (one thread per terminal - wall time
   is the slowest terminal, not the sum). Fast: instant ASMR tap + pop,
   immediate "closing..." toast, no artificial waits. */
async function closeAllBoth(el){
 if(INFLIGHT['caall'])return;INFLIGHT['caall']=true;
  const b=el||$id('closeAllTopBtn');if(b){b.disabled=true;asmrPop(b)}
  toast('closing ALL (both accounts)...',true);
  try{const j=await post('/api/close-all',{symbol:'ALL'});
   toast((j.detail||'close-all sent')+'  -  '+(j.wall_ms||0)+' ms wall',true);refresh()}
 catch(e){toast('CLOSE ALL: '+e.message,false)}
 finally{INFLIGHT['caall']=false;if(b)b.disabled=false}}
/* ---------- StopExe straddle (buy stop + sell stop around live) ----------
   Search runs on the broker feed (live quotes only): the universe is the
   account's spreads map, freshest quote wins.  Bracket math lives
   server-side (/api/stopexe/preview) so the panel and EXECUTE agree. */
function stexUniverse(n){
  const a=(P.accounts||{})[n]||{};const sp=a.spreads||{};
  return Object.keys(sp)}
/* live [bid,ask,ts] row for a symbol from the panel's spots feed
   (case-insensitive; null when the symbol is not streaming) */
function spotRow(sym){
  const s=(P&&P.spots)||{};
  sym=String(sym||'').trim();if(!sym)return null;
  let r=s[sym];
  if(!r){const up=sym.toUpperCase();
   for(const k in s){if(k.toUpperCase()===up){r=s[k];break}}}
  return (r&&r[0])?r:null}
function stexSel(n){const el=$id('stexsym'+n);
  return el?String(el.textContent||'').trim():''}
function stexSetSel(n,sym){
  const lab=$id('stexsym'+n);if(lab)lab.textContent=sym||'-';
  const box=$id('stexlist'+n);if(box)box.classList.remove('open');
  const inp=$id('stexsearch'+n);if(inp&&sym)inp.value=sym;
  stexPreview(n,true)}
function stexPaintQuote(n,sym,bid,ask){
  if(stexSel(n)!==sym)return;
  const b=$id('stexbid'+n),a=$id('stexask'+n);
  if(b)b.textContent=bid==null?'-':bid;
  if(a)a.textContent=ask==null?'-':ask}
let STEX_HITS={};
function stexPick(n,i){const h=STEX_HITS[n]||[];
  if(h[i])stexSetSel(n,h[i])}
function stexFilter(n){
  const q=String(($id('stexsearch'+n)||{}).value||'').trim().toUpperCase();
  const box=$id('stexlist'+n);if(!box)return;
  const uni=stexUniverse(n);
  const hits=uni.filter(s=>!q||s.toUpperCase().indexOf(q)>=0).slice(0,14);
  STEX_HITS[n]=hits;
  if(!hits.length){box.classList.remove('open');box.innerHTML='';return}
  const cur=stexSel(n);
  box.innerHTML=hits.map((s,i)=>'<div class="opt'+(s===cur?' on':'')+
   '" onclick="stexPick('+n+','+i+')">'+
   '<span class="mono">'+esc(s)+'</span><span class="pr">tap to arm</span></div>').join('');
  box.classList.add('open')}
function stexPips(n){const el=$id('stexpips'+n);
  const v=parseFloat(el&&el.value);return (v>=0.5&&v<=100)?v:0.5}
function stexCushion(n){const el=$id('stexspr'+n);
  const v=parseFloat(el&&el.value);return (v>=0&&v<=100)?v:0}
function stexJump(n){const b=$id('stexbody'+n);
  if(b)b.scrollIntoView({behavior:'smooth',block:'start'});
  const inp=$id('stexsearch'+n);if(inp)inp.focus({preventScroll:true})}
function stexStepLot(n,d){const el=$id('stexlot'+n);if(!el)return;
  let v=Math.max(0.01,(parseFloat(el.value)||0.01)+d);el.value=v.toFixed(2)}
let STEX_TS={};
async function stexPreview(n,force){
  /* the 0.6 s panel refresh calls stexLive(): throttle the preview to one
     request per 2 s per account - the bracket barely moves inside that */
  const now=Date.now();
  if(!force&&now-(STEX_TS[n]||0)<2000)return;
  STEX_TS[n]=now;
   const sym=stexSel(n);
   const buy=$id('stexbuy'+n),sell=$id('stexsell'+n);
   const bsl=$id('stexbuysl'+n),ssl=$id('stexsellsl'+n);
   const bslp=$id('stexbuyslp'+n),sslp=$id('stexsellslp'+n);
   const sprd=$id('stexspread'+n);
   if(!sym||sym==='-'){if(buy)buy.textContent='-';if(sell)sell.textContent='-';
    if(bsl)bsl.textContent='-';if(ssl)ssl.textContent='-';
    if(bslp)bslp.textContent='-';if(sslp)sslp.textContent='-';
    if(sprd)sprd.textContent='-';return}
   try{const j=await api('/api/stopexe/preview?account='+n+
    '&symbol='+encodeURIComponent(sym)+'&pips='+stexPips(n)+
    '&spread_cushion='+stexCushion(n));
   if(stexSel(n)!==sym)return;             // stale reply after re-arm
   if(buy)buy.textContent=j.buy_price;
   if(sell)sell.textContent=j.sell_price;
   if(bsl)bsl.textContent=j.buy_sl==null?'-':j.buy_sl;
   if(ssl)ssl.textContent=j.sell_sl==null?'-':j.sell_sl;
   if(bslp)bslp.textContent=fmt(j.sl_pips,2);
   if(sslp)sslp.textContent=fmt(j.sl_pips,2);
   if(sprd)sprd.textContent=fmt(j.spread_pips,2)+' pips';
   const br=$id('stexbr'+n);
   const pu=(+j.distance_pips===1)?' pip':' pips';
   if(br)br.textContent='±'+j.distance_pips+pu;
   const bk=$id('stexbuyk'+n);
   if(bk)bk.textContent='Buy stop · ask + '+j.distance_pips+pu;
   const sk=$id('stexsellk'+n);
   if(sk)sk.textContent='Sell stop · bid - '+j.distance_pips+pu;
   stexPaintQuote(n,sym,String(j.bid),String(j.ask));
  }catch(e){/* feed gap - keep the last bracket, refresh() retries */}}
function stexLive(n){
  const sym=stexSel(n);if(!sym||sym==='-')return;
  stexPreview(n)}
async function doStopexe(n){
  if(INFLIGHT['sx'+n])return;
  const sym=stexSel(n),fb=$id('stexfb'+n),btn=$id('stexbtn'+n);
  const lot=parseFloat(($id('stexlot'+n)||{}).value);
  if(!sym||sym==='-'){if(fb){fb.className='feedback err';
   fb.textContent='pick a pair first - search the broker feed above';}return}
  if(!(lot>0)){if(fb){fb.className='feedback err';
   fb.textContent='lot must be greater than 0';}return}
  INFLIGHT['sx'+n]=true;if(btn)btn.disabled=true;
  if(btn)asmrPop(btn);
   if(fb){fb.className='feedback';
   fb.textContent='STOPEXE '+sym+' '+lot.toFixed(2)+' ±'+stexPips(n)+' pips + 0.5-pip SL (floored past the live spread) ... placing both legs';}
   try{const j=await post('/api/stopexe',{account:+n,symbol:sym,lot:lot,
    distance_pips:stexPips(n),sl_pips:0.5,spread_cushion:stexCushion(n)});
   if(fb){fb.className='feedback ok';
    fb.textContent='ARMED '+j.detail+' · SL '+fmt(j.sl_pips,2)+' pips ('+j.ms+' ms) - CLOSE deletes both legs';}
   toast('STOPEXE '+sym+': buy '+j.buy_price+' (SL '+j.buy_sl+') / sell '+j.sell_price+' (SL '+j.sell_sl+')',true);
   refresh()}
  catch(e){if(fb){fb.className='feedback err';fb.textContent='REJECTED: '+e.message}
   toast('STOPEXE: '+e.message,false)}
  finally{delete INFLIGHT['sx'+n];if(btn)btn.disabled=false}}
/* ---------- pending stop/limit orders (StopExe straddle + ORDER tickets) --- */
function pendTable(n){const a=(P.accounts||{})[n]||{};const ps=a.pendings||[];
  const cnt=$id('pendcnt'+n);if(cnt)cnt.textContent=String(ps.length);
  const box=$id('pend'+n);if(!box)return;
  if(!ps.length){box.innerHTML='<div class="empty">no pending orders</div>';return}
  box.innerHTML='<table><tr><th>ticket</th><th>symbol</th><th>type</th>'+
   '<th class="num">vol</th><th class="num">level</th><th>placed</th><th></th></tr>'+
   ps.map(p=>'<tr><td class="mono mut">'+esc(p.ticket)+'</td>'+
    '<td class="mono">'+esc(p.symbol)+'</td>'+
    '<td><span class="side '+(String(p.type||'').indexOf('BUY')===0?'BUY':'SELL')+'">'+esc(p.type)+'</span></td>'+
    '<td class="num">'+fmt(Math.abs(+p.volume||0),2)+'</td>'+
    '<td class="num">'+esc(p.price)+'</td>'+
    '<td class="mut" style="font-size:11px">'+esc(String(p.time||'').slice(0,19))+'</td>'+
    '<td><button class="xbtn" onclick="cancelPending('+n+',\\''+esc(p.ticket)+'\\')">DELETE</button></td></tr>').join('')+
   '</table>'}
async function cancelPending(n,ticket){
  if(INFLIGHT['d'+ticket])return;INFLIGHT['d'+ticket]=true;
  try{const j=await post('/api/close',{account:+n,ticket:String(ticket)});
   toast('deleted #'+ticket+' - '+(j.detail||'ok'),true);refresh()}
  catch(e){toast(e.message,false)}
  finally{delete INFLIGHT['d'+ticket]}}
async function restartTerm(n){
 if(INFLIGHT['r'+n])return;INFLIGHT['r'+n]=true;
 toast('restarting terminal '+n+' ...',true);
 try{const j=await post('/api/restart',{account:+n});
  toast(j.detail||('terminal '+n+' restarting'),true);refresh()}
 catch(e){toast(e.message,false)}
 finally{setTimeout(()=>{delete INFLIGHT['r'+n]},15000)}}
/* ---------- pending-order ticket (BUY/SELL LIMIT & STOP) ----------
   The level is entered the same way the schedule form takes prices: a whole
   number plus the decimal digits, so EURUSD 1.12165 is 1 + 12165 and
   XAUUSD 4185.01 is 4185 + 01.  On open we prefill both from the live bid. */
function splitLevel(v){v=String(v==null?'':v).trim();
 if(!v.includes('.'))return[String(parseInt(v||'0',10)),''];
 const p=v.split('.');return[p[0],p[1]]}
function otypeHint(n){
 const t=($id('otyp'+n)||{}).value||'';
 const b=$id('owl'+n)&&$id('owl'+n).value, f=$id('ofr'+n)&&$id('ofr'+n).value;
 const lvl=(b+'')+((f!==''&&f!==undefined)?('.'+f):'');
 if(t.indexOf('BUY LIMIT')===0)return 'BUY LIMIT '+lvl+' - fills when price drops to the level';
 if(t.indexOf('BUY STOP')===0) return 'BUY STOP '+lvl+' - fills when price rises to the level';
 if(t.indexOf('SELL LIMIT')===0)return 'SELL LIMIT '+lvl+' - fills when price rises to the level';
 if(t.indexOf('SELL STOP')===0) return 'SELL STOP '+lvl+' - fills when price drops to the level';
 return ''}
function orderModalHTML(n){
 return '<div class="modal-head"><h3>Place pending order · account '+n+'</h3>'+
  '<button class="closex" onclick="closeModal()">×</button></div>'+
  '<div class="frow">'+
   '<div class="fgroup" style="grid-column:span 2"><label>Pair · search the broker feed</label>'+
    '<div class="symsearch"><input type="text" id="osearch'+n+'" placeholder="type to filter - e.g. XAU, EUR..." autocomplete="off" oninput="symSearch(\\'osym'+n+'\\',\\'ohits'+n+'\\',\\'osearch'+n+'\\')"></div>'+
    '<div class="symlist" id="ohits'+n+'"></div>'+
    '<select id="osym'+n+'"></select></div>'+
   '<div class="fgroup" style="grid-column:span 2"><label>Order type</label>'+
    '<select id="otyp'+n+'">'+['BUY LIMIT','BUY STOP','SELL LIMIT','SELL STOP']
      .map(t=>'<option>'+t+'</option>').join('')+'</select></div>'+
   '<div class="fgroup"><label>Lot size</label>'+
    '<input type="number" id="olot'+n+'" step="0.01" min="0.01" value="0.01"></div>'+
   '<div class="fgroup"><label>Orders</label>'+
    '<input type="number" id="onum'+n+'" step="1" min="1" max="50" value="1"></div>'+
   '<div class="fgroup"><label>Level · whole</label>'+
    '<input type="number" id="owl'+n+'" step="1" min="0" inputmode="numeric" placeholder="e.g. 1"></div>'+
   '<div class="fgroup"><label>Level · decimal</label>'+
    '<input type="text" id="ofr'+n+'" inputmode="numeric" autocomplete="off" placeholder="e.g. 12165"></div>'+
  '</div>'+
  '<div class="chiprow" id="ochipm'+n+'">'+
   [1,2,3,5,10].map(v=>'<button type="button" class="lchip'+(v===1?' on':'')+'" onclick="setONum('+n+','+v+')">'+v+'</button>').join('')+
  '</div>'+
  '<div class="secthint" id="ohint'+n+'">level = whole + decimal, with as many digits as the pair quotes</div>'+
  '<div style="margin-top:14px;display:flex;gap:10px;align-items:center;justify-content:center">'+
   '<button class="btn order-mini" id="oBtn'+n+'" onclick="submitOrder('+n+')">◌ PLACE ORDER</button>'+
   '<span class="feedback" style="margin:0" id="ofb'+n+'">GTC · sits on the broker until it triggers</span>'+
  '</div>'}
function setONum(n,v){const el=$id('onum'+n);if(!el)return;
 el.value=Math.max(1,Math.min(50,v|0||1));
 for(const c of document.querySelectorAll('#ochipm'+n+' .lchip'))
  c.className='lchip'+(c.textContent==String(el.value)?' on':'')}
function openOrderModal(n){
 MODAL_ACC=n;
 $id('modalBody').innerHTML=orderModalHTML(n);
 const sel=$id('osym'+n);
 const fromPanel=(((P||{}).accounts||{})['1']||{}).spreads||{};
 const paint=function(keys){
  const cur=sel.value;
  sel.innerHTML=keys.map(k=>'<option'+(k===cur?' selected':'')+'>'+esc(k)+'</option>').join('');
  if(cur&&keys.includes(cur))sel.value=cur;
 };
 paint(Object.keys(fromPanel));
 $id('modalBg').classList.add('open');
 const fill=function(){
  const sym=sel.value;if(!sym)return;
  api('/api/spots').then(function(j){
   const row=(j.spots||{})[sym];if(!row||!row.bid)return;
   const wf=splitLevel(row.bid);
   const w=$id('owl'+n),f=$id('ofr'+n);
   if(w)w.value=wf[0];if(f)f.value=wf[1];
   $id('ohint'+n).innerHTML=otypeHint(n)+'  ·  <b>live bid '+esc(row.bid)+
    '</b> ask '+esc(row.ask);
  }).catch(function(){})};
 // working even when the panel cache is stale: top up the pair list live
 api('/api/spots').then(function(j){
  const keys=Object.keys(j.spots||{}).filter(k=>j.spots[k]&&j.spots[k].bid);
  if(keys.length)paint(keys);
  fill();
 }).catch(function(){fill()});
 sel.onchange=fill;
 $id('otyp'+n).onchange=function(){$id('ohint'+n).innerHTML=otypeHint(n)};
 const onm=$id('onum'+n);
 if(onm)onm.oninput=function(){setONum(n,parseInt(onm.value,10)||1)};
 fill()}
async function submitOrder(n){
 if(INFLIGHT['o'+n])return;INFLIGHT['o'+n]=true;
  const b=$id('oBtn'+n),f=$id('ofb'+n);
  if(b){asmrPop(b);b.disabled=true}
  const sym=($id('osym'+n)||{}).value||'',typ=($id('otyp'+n)||{}).value||'';
 const lot=parseFloat(($id('olot'+n)||{}).value);
 const whole=($id('owl'+n)||{}).value,frac=($id('ofr'+n)||{}).value;
 let cnt=parseInt((($id('onum'+n)||{}).value||'1'),10);
 if(!(cnt>=1&&cnt<=50))cnt=1;
 if(!(lot>0)){f.className='feedback err';f.textContent='lot must be > 0';
  toast('lot must be > 0',false);if(b)b.disabled=false;delete INFLIGHT['o'+n];return}
 f.className='feedback';f.textContent='placing '+typ+' '+sym+(cnt>1?' x'+cnt:'')+' ...';
  try{const j=await post('/api/order',{account:+n,symbol:sym,type:typ,lot:lot,
    level_whole:whole,level_frac:frac,n:cnt});
   f.className='feedback ok';
  const got=(j.n||j.ok||1), tk=j.tickets||(j.ticket?[j.ticket]:[]);
  f.textContent='PLACED '+(got>1?got+'/'+cnt+' ':'#'+j.ticket+' ')+typ+' '+sym+' @ '+j.price+' ('+j.ms+' ms)';
  toast(typ+' '+sym+(cnt>1?' x'+cnt:'')+' @ '+j.price+' placed'+
   (tk.length>1?' - '+tk.length+' tickets':' - ticket '+j.ticket),true);
  setTimeout(closeModal,900);refresh()}
 catch(e){f.className='feedback err';f.textContent=e.message;toast(e.message,false)}
 finally{if(b)b.disabled=false;delete INFLIGHT['o'+n]}}

/* ---------- future-trade modal (shared dial-picker version in COMMON_JS) ---------- */
function openModal(n){MODAL_ACC=n;openSchedModal(n)}
function closeModal(){$id('modalBg').classList.remove('open');MODAL_ACC=null}
$id('modalBg').addEventListener('click',function(e){
 /* a click-through while a save is in flight used to close the modal and
    leave the operator staring at the schedule page - looking like the save
    was lost - so they opened the modal and saved AGAIN (duplicate). */
 if(e.target===this&&!SCHED_SAVING)closeModal()});

async function delSchedule(id){try{await post('/api/schedule/delete',{id});
  toast('schedule #'+id+' removed',true);refresh()}catch(e){toast(e.message,false)}}
function posTable(n){const a=(P.accounts||{})[n]||{};const ps=a.positions||[];
 $id('np'+n).textContent=ps.length;
 let pl=0;for(const p of ps)pl+=(+p.pl||0);
 const e=$id('pospl'+n);e.textContent=signed(pl);e.className=cls(pl);
 if(!ps.length){$id('pos'+n).innerHTML='<div class="empty">no open positions</div>';return}
 $id('pos'+n).innerHTML='<table><tr><th>ticket</th><th>symbol</th><th>side</th>'+
  '<th class="num">vol</th><th class="num">open</th><th class="num">now</th>'+
  '<th class="num">P/L</th><th class="num">swap</th><th>opened</th><th></th></tr>'+
  ps.map(p=>'<tr><td class="mono mut">'+esc(p.ticket)+'</td>'+
   '<td class="mono">'+esc(p.symbol)+'</td>'+
   '<td><span class="side '+esc(p.side)+'">'+esc(p.side)+'</span></td>'+
   '<td class="num">'+fmt(Math.abs(+p.volume||0),2)+'</td>'+
   '<td class="num">'+esc(p.open)+'</td><td class="num">'+esc(p.cur)+'</td>'+
   '<td class="num '+cls(+p.pl)+'">'+signed(+p.pl)+'</td>'+
   '<td class="num mut">'+signed(+p.swap)+'</td>'+
   '<td class="mut" style="font-size:11px">'+esc(String(p.time).slice(0,19))+'</td>'+
   '<td><button class="xbtn" onclick="closePos('+n+',\\''+esc(p.ticket)+'\\')">CLOSE</button></td></tr>').join('')+
  '</table>'}
function updAcc(n){const a=(P.accounts||{})[n]||{};
 if(!$id('live'+n))return;                     // panel not built yet
 /* login 0 = MT5's logged-out marker: show LOGOUT, never fake LIVE + 0.00 */
 const loggedOut=(String(a.login||'')===''||String(a.login||'')==='0');
 const syncing=!!a.syncing;
 $id('live'+n).className=syncing?'dot warn':dot(a.live&&!loggedOut,num(a.age_s)>0&&num(a.age_s)<60);
 $id('login'+n).textContent=a.login||'-';
 $id('server'+n).textContent=a.server||'-';
  $id('broker'+n).textContent=a.broker||'-';
  $id('cur'+n).textContent=a.currency||'';
 $id('bal'+n).textContent=fmt(a.balance);
 $id('eq'+n).textContent=fmt(a.equity);
 $id('eq'+n).className='v sm '+cls(num(a.equity)-num(a.balance));
 $id('pl'+n).textContent=signed(a.profit);
 $id('pl'+n).className='v sm '+cls(num(a.profit));
 $id('mg'+n).textContent=fmt(a.margin);
 $id('mgf'+n).textContent=fmt(a.margin_free);
 $id('mgl'+n).textContent=num(a.margin_level)>0?fmt(a.margin_level,1)+'%':'-';
 $id('lev'+n).textContent=syncing?'syncing...':(a.leverage||'-');
 const sp=a.spreads||{};
 $id('spread'+n).innerHTML=(syncing?'<span class="chip" style="border-color:var(--yellow);color:var(--yellow)">SYNC <b>PENDING</b></span>':'')+['EURUSD','GBPUSD','XAUUSD']
  .filter(k=>sp[k]!=null)
  .map(k=>'<span class="chip">'+k+' <b>'+fmt(sp[k],0)+' pts</b></span>').join('')+
  '<span class="chip">FEED <b>'+(a.live?'LIVE':'STALE')+'</b></span>';
  const sym=$id('sym'+n).value;
  if(sym&&sp[sym]!=null){$id('tspr'+n).textContent=fmt(sp[sym],0)+' pts'}
  /* MT5-style execution buttons: SELL carries the live BID, BUY the live
     ASK (spots rows are [bid,ask,ts]); greyed + disabled while this
     account's feed is not live so nobody fires into a dead terminal. */
  const row=spotRow(sym);
  const canTrade=!!(a.live&&!loggedOut&&!syncing&&row&&row[0]);
  for(const bid of ['sellbtn'+n,'buybtn'+n]){
   const b=$id(bid);if(b){b.classList.toggle('off',!canTrade);b.disabled=!canTrade}}
  const spx=$id('sellpx'+n),apx=$id('buypx'+n);
  if(spx)spx.textContent=canTrade?String(row[0]):'-';
  if(apx)apx.textContent=canTrade?(row[1]||'-'):'-';
  posTable(n);pendTable(n);stexLive(n)}
function renderSchedCount(n){const rows=(SCH||[]).filter(s=>String(s.account)===String(n));
 const cnt=$id('schcnt'+n);
 if(cnt)cnt.textContent=String(rows.filter(s=>s.active).length)}
function render(){if(!P)return;
  if(!BUILT){buildPanel('1');buildPanel('2');fillSymbols();
   for(const n of ['1','2']){
    $id('lchips'+n).innerHTML=[0.01,0.05,0.10,0.50,1.00]
     .map(v=>'<button class="lchip" onclick="setLot('+n+','+v.toFixed(2)+')">'
      +v.toFixed(2)+'</button>').join('');
     $id('ochips'+n).innerHTML=[1,2,3,5,10]
      .map(v=>'<button class="lchip'+(v===1?' on':'')+'" onclick="setOrd('+n+','+v+')">'
       +v+'</button>').join('');
     const oe=$id('ordn'+n);if(oe)oe.oninput=function(){markOrdChip(n)};
     /* StopExe default pair: gold first (the desk's straddle pair),
        else the first quoted symbol - preview arms on next refresh */
     const uni=stexUniverse(n);
     const dflt=uni.includes('XAUUSD')?'XAUUSD':(uni[0]||'-');
     const lab=$id('stexsym'+n);if(lab&&(lab.textContent==='-'||!lab.textContent))
      lab.textContent=dflt}
    BUILT=true}
  fillSymbols();updAcc('1');updAcc('2');
  renderSchedCount('1');renderSchedCount('2');
  const sys=P.system||{};liveLink(sys);}
async function refresh(){
  try{const results=await Promise.all([api('/api/panel'),api('/api/schedules')]);
   P=results[0];SCH=results[1].schedules||[];render()}
 catch(e){linkOffline();
  for(const n of ['1','2']){const c=$id('acc'+n);
   if(c&&c.children.length===0)
    c.innerHTML='<div class="empty">waiting for data - '+esc(e.message)+'</div>'}}}
refresh();setInterval(refresh,600);
/* ---------- NEWS module ----------
   Slides the coming week of releases (factory's ForexFactory feed via
   /api/news/events), draws the event's country from factory's Natural
   Earth borders (/api/news/map) and arms a StopExe straddle 5 s before
   the release (/api/news/plan).  With CLOSER on, the account is closed
   through close.py's fast path 5 s after the release - a 10 s window. */
let NW={open:false,acc:1,idx:0,high:true,closer:false,events:[],map:{},
        pending:{},quotes:{},sel:{symbol:'',lot:0.01,buy:1,sell:1},plans:[],
        busy:false,timer:null,plansTs:0,key:'',retry:null,eventCloser:false};
function newsEvent(){return NW.events[NW.idx]||null}
function newsKey(e){return String(e.dt_ms)+'|'+String(e.title)}
async function openNews(n){
  NW.open=true;NW.acc=(+n===2)?2:1;NW.idx=0;NW.key='';NW.plansTs=0;
  $id('newsBg').classList.add('open');
  newsPaintCloser();newsControls();
  await Promise.all([newsLoad(true),newsQuotes()]);
  newsLoadPlans();
  if(NW.timer)clearInterval(NW.timer);
  NW.timer=setInterval(newsTick,1000);newsTick();
}
function closeNews(){
  NW.open=false;$id('newsBg').classList.remove('open');
  if(NW.timer){clearInterval(NW.timer);NW.timer=null}
}
function newsDecline(){const e=newsEvent();closeNews();
  if(e)toast('declined: '+e.title,true)}
function newsToggleHigh(){
  NW.high=!NW.high;
  const c=$id('newsHighChip');
  if(c)c.className='chip filter'+(NW.high?' on':'');
  newsLoad(true)
}
function newsToggleCloser(){NW.eventCloser=!NW.eventCloser;newsPaintCloser();newsPaintQuote()}
function newsPaintCloser(){
  const b=$id('newsCloserBtn');
  if(b){b.className='news-closer'+(NW.eventCloser?' on':'');
    b.setAttribute('aria-pressed',NW.eventCloser?'true':'false')}
  const l=$id('newsCloserLbl');if(l)l.textContent=NW.eventCloser?'ON':'OFF'
}
async function newsLoad(force){
  try{
    const j=await api('/api/news/events?impact='+(NW.high?'high':'all')+
      (force?'&force=1':''));
    NW.events=j.events||[];
    const src=$id('newsSrc');
    if(src)src.textContent=(j.source||'calendar')+(j.loading?' · refreshing':'');
    const cnt=$id('newsCount');if(cnt)cnt.textContent=String(NW.events.length);
    if(NW.key){const i=NW.events.findIndex(function(e){return newsKey(e)===NW.key});
      if(i>=0)NW.idx=i}
    if(NW.idx>NW.events.length-1)NW.idx=Math.max(0,NW.events.length-1);
    newsPaint();
    /* first open after a boot: the feed can still be fetching - poll a few
       times so the panel fills itself instead of showing an empty week */
    if(!NW.events.length&&j.loading){
      clearTimeout(NW.retry);
      NW.retry=setTimeout(function(){if(NW.open)newsLoad(false)},3000)
    }
  }catch(e){
    const c=$id('newsCard');
    if(c)c.innerHTML='<div class="empty">calendar offline - '+esc(e.message)+
      '</div>'
  }
}
function newsSlide(d){
  if(!NW.events.length)return;
  NW.idx=(NW.idx+d+NW.events.length)%NW.events.length;
  NW.eventCloser=false; // closer OFF when sliding to a new event
  newsPaint()
}
function newsPaint(){
  const e=newsEvent(),card=$id('newsCard');
  const pos=$id('newsPos');
  if(pos)pos.textContent=NW.events.length?((NW.idx+1)+' / '+NW.events.length):'-';
  const tb=$id('newsTradeBtn');
  if(!e){
    if(card)card.innerHTML='<div class="empty">no '+
      (NW.high?'high impact ':'')+'news in the next week - '+
      '<button class="btn mini" onclick="newsToggleHigh()">'+
      (NW.high?'SHOW ALL IMPACTS':'HIGH IMPACT ONLY')+'</button></div>';
    if(tb)tb.disabled=true;
    return
  }
  NW.key=newsKey(e);
  if(tb)tb.disabled=false;
  const d=new Date(e.dt_ms);
  const when=d.toLocaleTimeString('en-GB',{hour12:false});
  const day=d.toLocaleDateString('en-GB',{weekday:'short',day:'2-digit',month:'short'});
  const utc=new Date(e.dt_ms).toISOString().slice(11,19);
  card.innerHTML=
   '<div class="news-map" id="newsMap"></div>'+
   '<div>'+
    '<div class="news-when">'+esc(when)+
      '<span class="tz">LOCAL · '+esc(day)+'</span></div>'+
    '<div class="news-metarow">'+
      '<span class="news-impact '+esc(e.impact||'Low')+'">'+esc(e.impact||'-')+'</span>'+
      '<span class="chip">'+esc(e.currency||'-')+' <b>'+esc(e.country||'-')+'</b></span>'+
      '<span class="chip">UTC <b>'+esc(utc)+'</b></span></div>'+
    '<div class="news-name">'+esc(e.title||'-')+'</div>'+
    '<div class="news-line">forecast <b>'+esc(e.forecast||'-')+
      '</b> · previous <b>'+esc(e.previous||'-')+'</b></div>'+
    '<div class="news-cd" id="newsCd">-</div>'+
   '</div>';
  newsPaintMap();newsTick()
}
function newsPaintMap(){
  const e=newsEvent(),box=$id('newsMap');
  if(!e||!box)return;
  const m=NW.map[e.currency];
  const ccy=e.currency||'--';
  const name=m&&m.name?m.name:countryName(ccy)||'';
  box.innerHTML='<span class="ccy">'+esc(ccy)+'</span>'+
   (m&&m.d
    ? '<svg viewBox="0 0 100 100" preserveAspectRatio="xMidYMid meet">'+
      '<path d="'+m.d+'" fill="#14304d" stroke="#4da6ff" stroke-width="0.8" '+
      'stroke-linejoin="round"/></svg>'
    : '<span class="none">'+(m?('no map for '+esc(ccy)):'drawing map...')+'</span>');
  if(!m&&!NW.pending[e.currency]){
    NW.pending[e.currency]=true;
    api('/api/news/map?ccy='+encodeURIComponent(e.currency)).then(function(j){
      NW.map[e.currency]=j;NW.pending[e.currency]=false;
      const cur=newsEvent();
      if(cur&&cur.currency===e.currency)newsPaintMap()
    }).catch(function(){NW.pending[e.currency]=false})
  }
  const lab=$id('newsMapLabel');if(lab)lab.remove();
  if(!m)return;
  const s=document.createElement('span');
  s.id='newsMapLabel';
  s.style.position='absolute';
  s.style.right='4px';s.style.top='4px';
  s.style.fontSize='10px';s.style.background='#14304d';s.style.color='#4da6ff';
  s.style.padding='2px 4px';s.style.borderRadius='3px';
  s.style.zIndex='10';s.textContent=name||'';
  box.parentNode.appendChild(s)
}
function countryName(ccy){
  const e=newsEvent();if(!e)return '';
  const c=(e.country||'').trim();return c?c:''
}
function newsTick(){
  const now=$id('newsNow');
  if(now)now.textContent=new Date().toLocaleTimeString('en-GB');
  const e=newsEvent(),cd=$id('newsCd');
  if(e&&cd){
    const s=Math.round((e.dt_ms-Date.now())/1000);
    let txt;
    if(s>86400)txt=Math.floor(s/86400)+'d '+Math.floor((s%86400)/3600)+'h '+
      Math.floor((s%3600)/60)+'m';
    else if(s>=3600)txt=Math.floor(s/3600)+'h '+Math.floor((s%3600)/60)+'m';
    else if(s>0)txt=s+'s';
    else txt='released '+(-s)+'s ago';
    cd.innerHTML=(s>0?'releases in <b>':'release <b>')+esc(txt)+
      (s>0?'</b> · straddle arms T-5 s (':('</b> · '))+'SL 0.5 pips'+
      (s>0?')':'')
  }
  if(Date.now()-(NW.plansTs||0)>5000)newsLoadPlans()
}
async function newsQuotes(){
  try{
    const j=await api('/api/news/quotes?account='+NW.acc);
    NW.quotes[NW.acc]=j.symbols||[];NW.qsrc=j.source||''
  }catch(e){NW.quotes[NW.acc]=[];NW.qsrc=''}
  const rows=NW.quotes[NW.acc]||[];
  if(!NW.sel.symbol||!rows.some(function(r){return r.symbol===NW.sel.symbol}))
    NW.sel.symbol=newsDefaultSymbol();
  newsControls();newsPaintQuote()
}
function newsDefaultSymbol(){
  const rows=NW.quotes[NW.acc]||[];
  const gold=rows.find(function(r){return /XAU|GOLD/i.test(r.symbol)});
  return (gold||rows[0]||{}).symbol||''
}
function newsControls(){
  const box=$id('newsControls');if(!box)return;
  const rows=NW.quotes[NW.acc]||[];
  const opts=rows.map(function(r){
    return '<option value="'+esc(r.symbol)+'">'+esc(r.symbol)+
      (r.bid==null?'':' · '+r.bid)+'</option>'
  }).join('');
  box.innerHTML=
   '<div class="fgroup"><label>Account</label><div class="seg">'+
     [1,2].map(function(n){return '<button class="btn mini'+
       (n===NW.acc?' blue':'')+'" onclick="newsSetAcc('+n+')">A'+n+'</button>'}).join('')+
   '</div></div>'+
   '<div class="fgroup"><label>Quote · probed live</label>'+
    '<input id="newsSym" list="newsSymList" autocomplete="off" spellcheck="false" '+
    'placeholder="search the broker feed" value="'+esc(NW.sel.symbol||'')+
    '" oninput="newsPickSym()" onchange="newsPickSym()">'+
    '<datalist id="newsSymList">'+opts+'</datalist></div>'+
   '<div class="fgroup"><label>Lot (each stop)</label>'+
    '<input id="newsLot" type="number" step="0.01" min="0.01" value="'+
    Number(NW.sel.lot||0.01).toFixed(2)+'" oninput="newsPickSym()"></div>'+
   '<div class="fgroup"><label>Sell stops</label>'+
    '<input id="newsOrdSell" type="number" step="1" min="0" max="50" value="'+
    Number(NW.sel.sell||0)+'" oninput="newsPickSym()"></div>'+
   '<div class="fgroup"><label>Buy stops</label>'+
    '<input id="newsOrdBuy" type="number" step="1" min="0" max="50" value="'+
    Number(NW.sel.buy||0)+'" oninput="newsPickSym()"></div>'
}
function newsPickSym(){
  const s=$id('newsSym'),l=$id('newsLot'),so=$id('newsOrdSell'),bo=$id('newsOrdBuy');
  if(s)NW.sel.symbol=String(s.value||'').trim();
  if(l)NW.sel.lot=parseFloat(l.value)||0.01;
  if(so)NW.sel.sell=Math.max(0,Math.min(50,parseInt(so.value,10)||0));
  if(bo)NW.sel.buy=Math.max(0,Math.min(50,parseInt(bo.value,10)||0));
  newsPaintQuote()
}
function newsSetAcc(n){NW.acc=(+n===2)?2:1;NW.sel.symbol='';newsQuotes();newsPaintPlans()}
function newsPaintQuote(){
  const box=$id('newsQuote');if(!box)return;
  const rows=NW.quotes[NW.acc]||[];
  const r=rows.find(function(x){return x.symbol===NW.sel.symbol});
  if(!r){
    box.innerHTML='<span class="chip">QUOTE <b>-</b></span>'+
      '<span class="mut" style="font-size:10px">'+(NW.sel.symbol?
      'not in the probed feed - pick an exact symbol from the list':
      'the broker probe has no tradeable symbol with a live quote on this account')+
      '</span>';
    return
  }
  const dig=(r.digits==null)?5:parseInt(r.digits,10);
  const spr=(r.ask&&r.bid)?(r.ask-r.bid).toFixed(dig):'-';
  box.innerHTML='<span class="chip">BID <b>'+esc(r.bid)+'</b></span>'+
    '<span class="chip">ASK <b>'+(r.ask==null?'-':esc(r.ask))+'</b></span>'+
    '<span class="chip">SPREAD <b>'+esc(spr)+'</b></span>'+
    '<span class="chip">STOPS <b>'+((NW.sel.buy||0)+(NW.sel.sell||0))+
      '</b> ('+(NW.sel.buy||0)+'B / '+(NW.sel.sell||0)+'S)</span>'+
    '<span class="chip">BRACKET <b>±0.5 pips</b></span>'+
    '<span class="chip">SL <b>0.5 pips</b></span>'+
    '<span class="chip">CLOSER <b>'+(NW.eventCloser?'T+5s':'off')+'</b></span>'+
    '<span class="mut" style="font-size:10px">'+esc(NW.qsrc||'')+'</span>'
}
async function newsTrade(){
  const e=newsEvent();
  if(!e){toast('no event selected - slide to one first',false);return}
  if(NW.busy)return;
  const rows=NW.quotes[NW.acc]||[];
  if(!rows.some(function(r){return r.symbol===NW.sel.symbol})){
    toast('pick an exact quote from the probed feed first',false);return}
  if((NW.sel.buy||0)+(NW.sel.sell||0)<1){
    toast('set at least one buy or sell stop',false);return}
  NW.busy=true;
  const btn=$id('newsTradeBtn');if(btn)btn.disabled=true;
  try{
    const j=await post('/api/news/plan',{account:NW.acc,symbol:NW.sel.symbol,
      lot:parseFloat(NW.sel.lot),
      orders:Math.max(NW.sel.buy||0,NW.sel.sell||0),
      buy_orders:NW.sel.buy||0,sell_orders:NW.sel.sell||0,
      closer:!!NW.eventCloser,event:{dt:e.dt,dt_ms:e.dt_ms,currency:e.currency,
      impact:e.impact,title:e.title,forecast:e.forecast,previous:e.previous,
      country:e.country}});
    toast(j.detail||'news bracket armed',true);
    newsLoadPlans()
  }catch(err){toast('NEWS: '+err.message,false)}
  finally{NW.busy=false;if(btn)btn.disabled=false}
}
async function newsLoadPlans(){
  if(!NW.open)return;
  NW.plansTs=Date.now();
  try{const j=await api('/api/news/plans');NW.plans=j.plans||[];newsPaintPlans()}
  catch(e){}
}
function newsPaintPlans(){
  const box=$id('newsPlans');if(!box)return;
  const rows=(NW.plans||[]).filter(function(p){
    return String(p.account)===String(NW.acc)||
      ['scheduled','arming','armed','closing'].indexOf(String(p.status))>=0
  });
  if(!rows.length){
    box.innerHTML='<span class="mut" style="font-size:10px">no news bracket '+
      'armed - TRADE arms the selected release at T-5 s</span>';
    return
  }
  box.innerHTML=rows.map(function(p){
    const t=new Date(p.event_ms);
    const st=String(p.status||'');
    const bad=/fail|expired|cancel/.test(st);
    const ev=p.event||{};
    return '<div class="prow"><span class="st'+(bad?' bad':'')+'">'+esc(st)+
      '</span><span class="mono">'+esc(p.symbol)+'</span>'+
      '<span class="mut">'+(p.buy_orders!=null?p.buy_orders:p.orders)+'B + '+
      (p.sell_orders!=null?p.sell_orders:p.orders)+'S x'+
      Number(p.lot).toFixed(2)+'</span>'+
      '<span class="mut">'+esc(t.toLocaleTimeString('en-GB'))+'</span>'+
      (p.closer?'<span class="chip">CLOSER</span>':'')+
      '<span class="mut" style="flex:1;min-width:110px;overflow:hidden;'+
      'text-overflow:ellipsis;white-space:nowrap">'+esc(ev.title||'')+'</span>'+
      '<button class="xbtn" data-plan="'+esc(p.id)+'">CANCEL</button></div>'
  }).join('');
  for(const b of box.querySelectorAll('button[data-plan]'))
    b.onclick=function(){newsCancel(this.getAttribute('data-plan'))}
}
async function newsCancel(id){
  try{
    const j=await post('/api/news/cancel',{id:String(id)});
    toast(j.detail||('news plan #'+id+' cancelled'),true);
    newsLoadPlans()
  }catch(e){toast(e.message,false)}
}
$id('newsBg').addEventListener('click',function(e){
  if(e.target===this)closeNews()
});
/* ---------------- account integration engine ---------------- */
function engState(s){
 if(s.running)return '<span style="color:var(--yellow,#eab308)">● '+esc(s.running.op)+'…</span>';
 if(s.verified)return '<span style="color:#22c55e">● synced</span>';
 if(s.phantom)return '<span style="color:#ef4444">● NOT LOGGED IN</span>';
 if(s.login)return '<span style="color:var(--yellow,#eab308)">● adopted '+esc(s.login)+'</span>';
 return '<span class="mut">○ logged out</span>'}
function engMode(s){
 if(!s.mode)return '';
 const live=String(s.mode).toLowerCase()==='live';
 const demo=String(s.mode).toLowerCase()==='demo';
 const col=live?'#ef4444':(demo?'#22c55e':'#eab308');
 return ' <span style="color:'+col+';font-weight:700">['+esc(String(s.mode).toUpperCase())+']</span>'}
function engRender(){
 if(!ENG)return;
 const slots=ENG.slots||[];
 $id('engSlots').innerHTML=slots.map(s=>{
  const srv=s.want_server||s.server||'?';
  /* An unknown trade server with no access point can never connect: MT5
     echoes the requested login but dials nothing. Say so here instead of
     letting the row look 'adopted'. */
  const warn=s.want_unreachable
   ?'<div class="mut sub" style="width:100%;color:#ef4444;padding-top:4px">'+
    'MT5 has never seen server '+esc(srv)+' and has no access point - '+
    'logins to it cannot connect '+
    '<button class="btn mini blue" onclick="engApOpen(\\''+esc(srv)+'\\')">ADD ACCESS POINT</button></div>'
   :(s.access_point?'<div class="mut sub" style="width:100%;padding-top:4px">'+
     'via access point '+esc(s.access_point)+'</div>':'');
  return '<div style="display:flex;flex-wrap:wrap;gap:8px;align-items:center;'+
   'padding:10px 0;border-bottom:1px solid color-mix(in srgb,currentColor 12%,transparent)">'+
   '<b>T'+s.terminal+'</b>'+
   '<span class="mono">'+esc(s.login||'-')+'</span>'+engMode(s)+
   '<span class="mut sub">'+esc(s.server||'-')+'</span>'+
   '<span class="mut sub">'+esc(s.broker||'-')+'</span>'+
   '<span class="sub">bal '+esc(s.balance||'-')+'</span>'+
   '<span class="sub">eq '+esc(s.equity||'-')+'</span>'+
   '<span class="mut sub">feed '+(s.feed_age_s!=null?s.feed_age_s+'s':'-')+'</span>'+
   '<span class="spacer" style="flex:1"></span>'+engState(s)+
   '<button class="btn mini" onclick="engLogin('+s.terminal+')">LOGIN</button>'+
   '<button class="btn mini" onclick="engAdopt('+s.terminal+')">ADOPT</button>'+
   '<button class="btn mini" onclick="engRestart('+s.terminal+')">RESTART</button>'+
   '<button class="btn mini danger" onclick="engLogout('+s.terminal+')">LOGOUT</button>'+
   warn+'</div>'}).join('');
 const busy=(ENG.slots||[]).some(s=>s.running);
 const chip=$id('engBusy');
 chip.textContent=busy?'SYNCING':'IDLE';
 chip.style.color=busy?'var(--yellow,#eab308)':'';}
async function engPoll(){
 try{const j=await api('/api/engine/status');
  ENG=j;ENGOPS=j.ops||[];engRender()}
 catch(e){/* supervisor may be down - keep the last frame */}}
function engRefresh(){engPoll();toast('engine status refreshed',true)}
async function engAct(path,body){
 try{const j=await post(path,body);
  toast(j.detail||'sent',true);engPoll();return j}
 catch(e){toast(e.message,false);return null}}
async function engAdopt(t){await engAct('/api/engine/adopt',{terminal:t})}
async function engRestart(t){await engAct('/api/engine/restart',{terminal:t})}
async function engLogout(t){
 if(!confirm('Log terminal '+t+' out and scrub its stored credentials?'))return;
 await engAct('/api/engine/logout',{terminal:t})}
/* inline login form: replaces the old blocking prompt() chain so the
   operator can pick a saved account, see the server, and keep the whole
   panel responsive while the terminal restarts into the new account. */
let ENGFORM_T=null;
async function engFillKnown(){
 const sel=$id('engKnownIn');if(!sel)return;
 try{const j=await api('/api/engine/accounts');
  const accs=(j.accounts||[]);
  sel.innerHTML='<option value="">- pick a saved account -</option>'+
   accs.map(a=>'<option value="'+esc(a.login)+'" data-server="'+esc(a.server||'')+'" '+
     'data-pw="'+(a.has_password?'1':'0')+'">'+esc(a.login)+
     (a.server?' \u00b7 '+esc(a.server):'')+(a.has_password?' \u00b7 saved pw':' \u00b7 no pw')+
     (a.label?' \u00b7 '+esc(a.label):'')+'</option>').join('');
 }catch(e){/* keep an empty picker if the book is unavailable */}}
function engPickKnown(){
 const sel=$id('engKnownIn');const o=sel.options[sel.selectedIndex];
 if(!o||!o.value)return;
 $id('engLoginIn').value=o.value;
 $id('engServerIn').value=o.getAttribute('data-server')||'';
 $id('engPassIn').value='';
 const fb=$id('engFeedback');
 fb.textContent=(o.getAttribute('data-pw')==='1')
  ?'saved password will be used - leave the password blank'
  :'no saved password - type the password to log in';
 fb.className='feedback'}
function engOpenForm(t){
 ENGFORM_T=t;
 $id('engFormT').textContent=String(t);
 $id('engForm').style.display='';
 $id('engFeedback').textContent='';
 $id('engFeedback').className='feedback';
 engFillKnown();
 const f=$id('engLoginIn');if(f)f.focus()}
function engCloseForm(){
 ENGFORM_T=null;$id('engForm').style.display='none'}
/* access point: the one-time fix for a broker MT5 does not list yet. */
let ENGAP_SERVER=null;
function engApOpen(server){
 ENGAP_SERVER=server;
 $id('engApServer').textContent=server;
 $id('engApForm').style.display='';
 $id('engApFeedback').textContent='';
 $id('engApFeedback').className='feedback';
 const f=$id('engApAddrIn');if(f){f.value='';f.focus()}}
function engApClose(){ENGAP_SERVER=null;$id('engApForm').style.display='none'}
async function engApSubmit(){
 if(ENGAP_SERVER==null)return;
 const addr=($id('engApAddrIn').value||'').trim();
 const fb=$id('engApFeedback');
 if(!addr){fb.textContent='host:port is required (e.g. mt5.cxmdirect.com:443)';
  fb.className='feedback err';return}
 fb.textContent='saving...';fb.className='feedback';
 try{const j=await post('/api/engine/access-point',
   {server:ENGAP_SERVER,access_point:addr});
  fb.textContent='';fb.className='feedback ok';
  toast(j.detail||'access point saved',true);engApClose();engPoll()}
 catch(e){fb.textContent=e.message;fb.className='feedback err'}}
async function engSubmitLogin(){
 if(ENGFORM_T==null)return;
 const login=($id('engLoginIn').value||'').trim();
 const server=($id('engServerIn').value||'').trim();
 const password=$id('engPassIn').value||'';
 const fb=$id('engFeedback');
 if(!login){fb.textContent='account number is required';
  fb.className='feedback err';return}
 if(!server){fb.textContent='server is required (e.g. HFM-Real or MetaQuotes-Demo)';
  fb.className='feedback err';return}
 fb.textContent='logging in...';fb.className='feedback';
 const j=await engAct('/api/engine/login',{terminal:ENGFORM_T,login:login,
  password:password,server:server});
 if(j){fb.textContent='';fb.className='feedback ok';engCloseForm()}}
function engLogin(t){engOpenForm(t)}
/* ---------- saved accounts: store once, log in with one click ---------- */
let ENGSAVED=null;
function engSavedRender(){
 const box=$id('engSaved');if(!box)return;
 if(!ENGSAVED){box.innerHTML='<div class="empty">loading...</div>';return}
 if(!ENGSAVED.length){box.innerHTML='<div class="empty">no saved accounts yet - press + SAVE ACCOUNT</div>';return}
 box.innerHTML=ENGSAVED.map(a=>
  '<div style="display:flex;flex-wrap:wrap;gap:8px;align-items:center;'+
   'padding:10px 0;border-bottom:1px solid color-mix(in srgb,currentColor 12%,transparent)">'+
   '<b class="mono">'+esc(a.login)+'</b>'+
   '<span class="mut sub">'+esc(a.server||'-')+'</span>'+
   (a.label?'<span class="sub">'+esc(a.label)+'</span>':'')+
   '<span class="sub" style="color:'+(a.has_password?'#22c55e':'#eab308')+'">'+
    (a.has_password?'● pw saved':'○ no pw')+'</span>'+
   '<span class="spacer" style="flex:1"></span>'+
   '<button class="btn mini blue" onclick="engSavedLogin(\\''+esc(a.login)+'\\',1)">T1 LOGIN</button>'+
   '<button class="btn mini blue" onclick="engSavedLogin(\\''+esc(a.login)+'\\',2)">T2 LOGIN</button>'+
   '<button class="btn mini" onclick="engSavedEdit(\\''+esc(a.login)+'\\')">EDIT</button>'+
   '<button class="btn mini danger" onclick="engSavedDel(\\''+esc(a.login)+'\\')">DEL</button>'+
  '</div>').join('')}
async function engPollSaved(){
 try{const j=await api('/api/engine/accounts');
  ENGSAVED=j.accounts||[];engSavedRender()}catch(e){}}
async function engSavedLogin(login,t){
 await engAct('/api/engine/login',{terminal:t,login:login,
  password:'',server:''})}
async function engSavedDel(login){
 if(!confirm('Forget saved account '+login+'? (terminals keep running)'))return;
 const j=await engAct('/api/engine/forget',{login:login});
 if(j)engPollSaved()}
function engToggleSave(hide){
 const f=$id('engSaveForm');if(!f)return;
 f.style.display=(hide||f.style.display!=='none')?'none':'';
 if(f.style.display!=='none'){
  $id('engSaveFeedback').textContent='';
  $id('engSaveFeedback').className='feedback';
  const l=$id('engSaveLoginIn');if(l)l.focus()}}
function engSavedEdit(login){
 const a=(ENGSAVED||[]).find(x=>String(x.login)===String(login));
 if(!a)return;
 $id('engSaveLoginIn').value=a.login;
 $id('engSaveServerIn').value=a.server||'';
 $id('engSavePassIn').value='';
 $id('engSaveLabelIn').value=a.label||'';
 const f=$id('engSaveForm');if(f)f.style.display='';
 $id('engSaveFeedback').textContent=a.has_password
  ?'leave the password blank to keep the saved one'
  :'no saved password - type one to store it';
 $id('engSaveFeedback').className='feedback'}
async function engSaveSubmit(){
 const login=($id('engSaveLoginIn').value||'').trim();
 const server=($id('engSaveServerIn').value||'').trim();
 const password=$id('engSavePassIn').value||'';
 const label=($id('engSaveLabelIn').value||'').trim();
 const fb=$id('engSaveFeedback');
 if(!/^\\d+$/.test(login)){fb.textContent='account number must be digits';
  fb.className='feedback err';return}
 if(!server){fb.textContent='server is required (e.g. FxPro-MT5)';
  fb.className='feedback err';return}
 fb.textContent='saving...';fb.className='feedback';
 try{const j=await post('/api/engine/save',{login:login,server:server,
   password:password,label:label});
  fb.textContent='';fb.className='feedback ok';
  $id('engSaveLoginIn').value='';$id('engSavePassIn').value='';
  $id('engSaveLabelIn').value='';
  toast(j.detail||'saved',true);engPollSaved();engFillKnown()}
 catch(e){fb.textContent=e.message;fb.className='feedback err'}}
engPoll();setInterval(engPoll,2000);
engPollSaved();setInterval(engPollSaved,10000);
"""

def render_panel() -> str:
    return render_template_string(PANEL_TMPL, css=CSS, js=PANEL_JS)

SCHEDULED_TMPL = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>MT5 TERMINAL - SCHEDULED TRADES</title>
<style>{{ css|safe }}</style></head>
<body>
<div class="layout">
  <aside class="sidebar">
    <div class="sb-brand"><div class="logo"></div>MT5<b>//</b>TERM</div>
    <div class="sb-sub">trading terminal</div>
    <div class="sb-section">Workspace</div>
    <a class="sb-item" href="/"><span class="ic">\u25c8</span>Dashboard</a>
    <a class="sb-item" href="/panel"><span class="ic">\u25a3</span>Trading Panel</a>
    <a class="sb-item on" href="/scheduled"><span class="ic">\u25c7</span>Scheduled Trades</a>
    <a class="sb-item" href="/factory"><span class="ic">\u25c8</span>Factory</a>
    <div class="sb-foot">SPOTDUMP EA \u00b7 v2</div>
  </aside>
  <main class="main">
    <header class="topbar">
      <div>
        <div class="page-title">SCHEDULED TRADES</div>
        <div class="page-sub">future trades &amp; past-24 h fired log \u00b7 live countdown \u00b7 times are YOUR clock</div>
      </div>
      <div class="spacer"></div>
      <div class="status-pill" id="sysPill"><span class="dot" id="sysdot"></span>
        <span id="sysLbl"></span></div>
      <div id="clock" class="mono"></div>
    </header>
    <div class="wrap">
      <section class="kpi-grid" id="kpiGrid"></section>
      <div class="grid" style="grid-template-columns:1fr;gap:14px">
        <section class="panel">
          <div class="sect"><h4>Schedules</h4>
            <div class="right">
              <span class="chip filter" id="fltALL" onclick="setFilter('ALL')">ALL <b id="cntALL">0</b></span>
              <span class="chip filter on" id="fltACTIVE" onclick="setFilter('ACTIVE')">ACTIVE <b id="cntACTIVE">0</b></span>
              <span class="chip filter" id="fltOFF" onclick="setFilter('OFF')">OFF <b id="cntOFF">0</b></span>
              <button class="btn mini blue" onclick="openModal(1)">+ NEW SCHEDULE</button></div></div>
          <div class="tblwrap" id="schedTbl"><div class="empty">loading...</div></div>
        </section>
        <section class="panel">
          <div class="sect"><h4>Fired Log</h4>
            <div class="right"><button class="btn mini danger" onclick="clearHistory()">CLEAR HISTORY</button>
            <button class="btn mini" id="seeAllBtn" onclick="toggleFired()">SEE ALL</button></div></div>
          <div class="tblwrap" id="firedTbl"><div class="empty">loading...</div></div>
        </section>
      </div>
    </div>
  </main>
</div>
<div class="modal-bg" id="modalBg">
  <div class="modal" id="modalBody"></div>
</div>
<div class="toast" id="toast"></div>
<script>{{ js|safe }}</script>
</body></html>"""

SCHEDULED_JS = COMMON_JS + """
let S=null,F=null,SYS=null,BUILT=false,MODAL_ACC=null,FIRED_EXPANDED=false,FILTER='ACTIVE';
const FIRED_PREVIEW = 20;  // show only last 20 by default
/* next_fire is stored UTC; show the operator their own wall clock */
function localFire(iso){try{return new Date(iso).toLocaleTimeString('en-GB')+
 ' \u00b7 '+new Date(iso).toLocaleDateString('en-GB')}catch(e){return String(iso||'')}}
let NEWS_PLANS=[];
/* News plans load in the background: the schedule table must paint even
   when /api/news/plans is slow (a blocking await here left the section on
   "loading..." forever). */
function loadNewsPlans(){
 api('/api/news/plans').then(function(j){
  NEWS_PLANS=j.plans||[];renderSched()
 }).catch(function(){})}
/* fired log = past 24 h only (the server also prunes old rows) */
function fresh24(rows){const cut=Date.now()-86400000;
 return (rows||[]).filter(r=>{try{return new Date(r.at).getTime()>=cut}catch(e){return true}})}
function hhmmss(h,m,s){return String(h).padStart(2,'0')+':'+String(m).padStart(2,'0')+':'+String(s).padStart(2,'0')}
function optRange(a,b,sel){let o='';for(let i=a;i<=b;i++){o+='<option value="'+i+'"'+
 (i==sel?' selected':'')+'>'+String(i).padStart(2,'0')+'</option>'}return o}
/* countdown: 'in 3 s' / 'in 2 m 10 s' / 'in 1 h 03 m' / 'tomorrow' */
function countdown(iso){try{const t=new Date(iso),now=new Date();
 let ds=Math.round((t-now)/1000);if(isNaN(ds))return '';
 if(ds<0)return 'due';
 if(ds>=86400)return 'tomorrow';
 const hh=Math.floor(ds/3600),mm=Math.floor(ds%3600/60),ss=ds%60;
 return 'in '+(hh?hh+' h '+String(mm).padStart(2,'0')+' m':(mm?mm+' m '+String(ss).padStart(2,'0')+' s':ss+' s'))}catch(e){return ''}}
function setFilter(f){FILTER=f;
 for(const k of ['ALL','ACTIVE','OFF']){const el=$id('flt'+k);
  if(el)el.className='chip filter'+(k===f?' on':'')}
 renderSched()}
function renderKpis(){
  const active=(S||[]).filter(s=>s.active);
  const fired=fresh24(F).length;
  const ok=fresh24(F).filter(r=>r.ok).length;
  const cards=[
   {k:'ACTIVE SCHEDULES',v:String(active.length),c:'',sub:'armed & counting down'},
   {k:'FIRED TOTAL',v:String(fired),c:'',sub:'executions in past 24 h'},
   {k:'FIRED OK',v:String(ok),c:'up',sub:'successful executions'},
   {k:'FIRED ERR',v:String(fired-ok),c:fired>ok?'down':'',sub:'failed executions'},
  ];
  $id('kpiGrid').innerHTML=cards.map(c=>
   '<div class="kpi"><div class="k">'+c.k+'</div>'+
   '<div class="v '+(c.c||'')+'">'+c.v+'</div>'+
   '<div class="sub">'+(c.sub||'')+'</div></div>').join('')}
function renderSched(){
  const cnt={ALL:(S||[]).length,ACTIVE:(S||[]).filter(s=>s.active).length,
   OFF:(S||[]).filter(s=>!s.active).length};
  for(const k in cnt){const el=$id('cnt'+k);if(el)el.textContent=String(cnt[k])}
  let rows=S||[];
  if(FILTER==='ACTIVE')rows=rows.filter(s=>s.active);
  if(FILTER==='OFF')rows=rows.filter(s=>!s.active);
  rows=rows.slice().sort((a,b)=>String(a.next_fire||'').localeCompare(String(b.next_fire||'')));
  const newsRows=NEWS_PLANS.map(p=>({
    id: 'news-'+p.id,
    news_id: String(p.id),
    news: true,
    account: p.account,
    pair: p.symbol,
    side: 'STRADDLE',
    lot: p.lot,
    n_positions: (p.buy_orders!=null?p.buy_orders:p.orders)+
      (p.sell_orders!=null?p.sell_orders:p.orders),
    exec_h: '', exec_m: '', exec_s: '',
    close_h: '', close_m: '', close_s: '',
    next_fire: p.event_ms?new Date(Number(p.event_ms)).toISOString():'',
    active: ['scheduled','arming','armed','closing'].indexOf(
      String(p.status||'scheduled'))>=0,
    status: String(p.status||'scheduled'),
    closer: p.closer||false,
  })).filter(s=>FILTER==='ALL'||(FILTER==='ACTIVE'?s.active:!s.active));
  window.__retryState={};
  for(const s of rows)if(s.slot_retry)window.__retryState[s.id]=s.slot_retry;
  if(!rows.length&&newsRows.length===0){$id('schedTbl').innerHTML='<div class="empty">'+
   (FILTER==='ACTIVE'?'no active schedules - create one with + NEW SCHEDULE (or arm one +30 s away)':'no schedules here')+'</div>';return}
  const allRows=[...rows,...newsRows].sort((a,b)=>String(a.next_fire||'').localeCompare(String(b.next_fire||'')));
  $id('schedTbl').innerHTML='<table><tr><th>#</th><th>account</th><th>pair</th><th>side</th>'+
   '<th class="num">lot</th><th class="num">x</th><th>open at</th><th>close at</th>'+
   '<th>next fire</th><th>status</th><th></th></tr>'+
   allRows.map(s=>'<tr><td class="mut">'+(s.news?'NEWS':s.id)+
    (s.slot_retry?'<div style="color:#eab308;font-weight:600">RETRYING x'+s.slot_retry.tries+
     ' <span class="cd" data-retry="'+s.id+'"></span></div>':'')+'</td><td>acc'+s.account+'</td>'+
    '<td class="mono">'+esc(s.pair)+'</td>'+
    '<td><span class="side '+esc(s.side)+'">'+esc(s.side)+'</span></td>'+
    '<td class="num">'+fmt(s.lot,2)+'</td><td class="num">'+s.n_positions+'</td>'+
    '<td class="mono">'+(s.exec_h?hhmmss(s.exec_h,s.exec_m,s.exec_s):'-')+'</td>'+
    '<td class="mono">'+(s.close_h?hhmmss(s.close_h,s.close_m,s.close_s):'-')+'</td>'+
    '<td class="mut" style="font-size:11px" data-fire="'+esc(String(s.next_fire||''))+'">'+(s.active?
     '<span class="cd up"></span> \u00b7 ':'')+
    esc(localFire(s.next_fire))+'</td>'+
    '<td>'+(s.news?'<span class="'+(s.active?'up':'mut')+'">'+esc(s.status.toUpperCase())+'</span>':
     (s.active?'<span class="up">ACTIVE</span>':'<span class="mut">OFF</span>'))+'</td>'+
    '<td style="white-space:nowrap">'+
    (s.news?'<button class="xbtn" data-news="'+esc(s.news_id)+'" onclick="cancelNewsPlan(this.dataset.news)">CANCEL</button>':
     '<button class="xbtn" onclick="editSchedule('+s.id+')">EDIT</button> '+
     '<button class="xbtn" onclick="toggleSchedule('+s.id+','+s.active+')">'+(s.active?'PAUSE':'ARM')+'</button> '+
     '<button class="xbtn" onclick="delSchedule('+s.id+')">DEL</button>')+'</td></tr>').join('')+
    '</table>'}
async function cancelNewsPlan(id){
  try{const j=await post('/api/news/cancel',{id:String(id)});
   toast(j.detail||('news plan #'+id+' cancelled'),true);refresh()}
  catch(e){toast(e.message,false)}}
function renderFired(){
  const all = fresh24(F);
  if(!all.length){$id('firedTbl').innerHTML='<div class="empty">no fired trades in the past 24 h</div>';return}
  const show = FIRED_EXPANDED ? all : all.slice(0, FIRED_PREVIEW);
  const btn = $id('seeAllBtn');
  if(btn) btn.textContent = FIRED_EXPANDED ? 'SHOW LESS'
    : (all.length>FIRED_PREVIEW ? 'SEE ALL (' + (all.length - FIRED_PREVIEW) + ' more)' : 'SEE ALL');
  /* COLLAPSE N-TUPLE FIRES: a schedule of n positions writes n fired rows
     in the same second - same schedule, pair, side, price - and that block
     read exactly like a duplicate schedule had fired.  Collapse every run
     of identical rows (OK and ERR alike) into ONE row with a 'x N' badge,
     so a 4-position schedule shows a single 4-position fire.  The detail
     string embeds the per-position ticket (price|ticket|volume), so the
     ticket is stripped before comparing - that is the ONLY way OK rows of
     one batch differ. */
  const collapsed=[];
  const stripTicket=(v,t)=>t?String(v||'').split(String(t)).join(''):String(v||'');
  for(const r of all){
   const last=collapsed[collapsed.length-1];
   if(last && last.schedule_id===r.schedule_id &&
      last.account===r.account && last.kind===r.kind &&
      last.pair===r.pair && last.side===r.side &&
      last.ok===r.ok &&
      String(last.at).slice(0,16)===String(r.at).slice(0,16) &&
      stripTicket(last.detail,last.ticket)===stripTicket(r.detail,r.ticket)){
    last._n=(last._n||1)+1}
   else collapsed.push(r)}
  $id('firedTbl').innerHTML='<table><tr><th>at</th><th>schedule</th><th>account</th><th>kind</th>'+
   '<th>pair</th><th>side</th><th class="num">lot</th><th>ticket</th><th>result</th>'+
   '<th>detail</th><th class="num">ms</th></tr>'+
   collapsed.map(r=>'<tr><td class="mut" style="font-size:11px">'+esc(String(r.at).slice(11,19))+'</td>'+
    '<td class="mut">#'+r.schedule_id+(r._n?' <span class="down">x'+r._n+'</span>':'')+'</td><td>acc'+r.account+'</td>'+
    '<td>'+esc(r.kind)+'</td><td class="mono">'+esc(r.pair)+'</td>'+
    '<td><span class="side '+esc(r.side)+'">'+esc(r.side)+'</span></td>'+
    '<td class="num">'+fmt(r.lot,2)+'</td><td class="mono mut">'+esc(r.ticket||'-')+'</td>'+
    '<td>'+(r.ok?'<span class="up">OK</span>':'<span class="down">ERR</span>')+'</td>'+
    '<td class="mut" style="font-size:11px;max-width:220px;overflow:hidden;text-overflow:ellipsis">'+esc(r.detail)+'</td>'+
    '<td class="num">'+(r.ms?fmt(r.ms,0):'-')+'</td></tr>').join('')+
   '</table>'}
function toggleFired(){
  FIRED_EXPANDED = !FIRED_EXPANDED;
  renderFired();
}
async function clearHistory(){
  if(!confirm('Wipe the fired log and remove OFF schedules? Active schedules are kept.'))return;
  try{const j=await post('/api/history/clear',{});
   toast(j.detail||('cleared '+j.deleted+' rows'),true);refresh()}
  catch(e){toast(e.message,false)}}
function editSchedule(id){const s=(S||[]).find(x=>x.id===id);if(!s)return;
 openSchedModal(s.account,{id:s.id,pair:s.pair,side:s.side,lot:s.lot,n:s.n_positions,
  h:s.exec_h,m:s.exec_m,s:s.exec_s,ch:s.close_h,cm:s.close_m,cs:s.close_s})}
async function toggleSchedule(id,isActive){
 try{const j=await post('/api/schedule/update',{id:id,active:!isActive});
  toast('schedule #'+id+(isActive?' paused':' armed')+' - next fire '+(j.next_fire_local||''),true);
  refresh()}catch(e){toast(e.message,false)}}
function render(){renderSched();renderKpis();renderFired();
 liveLink((SYS&&SYS.system)||{})}
async function refresh(){
  try{const [sch,fired,sys]=await Promise.all([api('/api/schedules'),api('/api/fired'),
   api('/api/dashboard')]);
   S=sch.schedules||[];F=fired.fired||[];SYS=sys;render()}
  catch(e){linkOffline()}}
/* ---------- future-trade modal (shared dial-picker version in COMMON_JS) ---------- */
function openModal(n){openSchedModal(n,null)}
function closeModal(){$id('modalBg').classList.remove('open');MODAL_ACC=null}
$id('modalBg').addEventListener('click',function(e){
 /* a click-through while a save is in flight used to close the modal and
    leave the operator staring at the schedule page - looking like the save
    was lost - so they opened the modal and saved AGAIN (duplicate). */
 if(e.target===this&&!SCHED_SAVING)closeModal()});
async function delSchedule(id){
  if(!confirm('Delete schedule #'+id+'? This removes it for good.'))return;
  try{await post('/api/schedule/delete',{id:id,hard:true});
  toast('schedule #'+id+' deleted',true);refresh()}catch(e){toast(e.message,false)}}
function fillSymbols(){
  /* fetch spot symbols for the pair dropdown */
  api('/api/spots').then(j=>{const spots=j.spots||{};const keys=Object.keys(spots);
   const sel=$id('sp'+(MODAL_ACC||1));if(!sel)return;
   /* rebuild from scratch: appending to the existing options duplicated
      the whole list every time the modal was reopened */
   const cur=sel.value;
   sel.innerHTML=keys.map(k=>'<option'+(k==cur?' selected':'')+'>'+k+'</option>').join('');
   if(cur&&keys.includes(cur))sel.value=cur;
   else if(cur)sel.innerHTML='<option>'+esc(cur)+'</option>'+sel.innerHTML}).catch(()=>{})}
function renderCountdowns(){
  const tds=document.querySelectorAll('#schedTbl td[data-fire]');
  if(tds.length)for(const td of tds){
   const sp=td.querySelector('.cd');
   if(sp)sp.textContent=countdown(td.getAttribute('data-fire'))}
  const rst=window.__retryState||{};
  for(const el of document.querySelectorAll('#schedTbl .cd[data-retry]')){
   const st=rst[el.getAttribute('data-retry')];
   if(st&&st.next_in!=null)el.textContent='\u00b7 next try '+st.next_in+'s'}}
setInterval(renderCountdowns,1000);
loadNewsPlans();setInterval(loadNewsPlans,5000);
refresh();setInterval(refresh,2000);
"""

def render_scheduled() -> str:
    return render_template_string(SCHEDULED_TMPL, css=CSS, js=SCHEDULED_JS)


# ---------------------------------------------------------------------------
# FACTORY - forex news globe + LED ticker (sits right below Scheduled Trades).
# The tkinter/PIL HUD from factory.py is re-rendered in the browser: static
# Natural Earth geometry comes from /api/factory/map once, the ForexFactory
# feed from /api/factory/news, and every frame (clock + globe) is painted
# locally on a canvas with requestAnimationFrame.  No per-frame round-trip,
# so the clock keeps sub-millisecond resolution and the globe stays smooth
# even when the network stalls ("enhanced latency" = local render loop).
# ---------------------------------------------------------------------------
FACTORY_TMPL = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>MT5 TERMINAL - FACTORY</title>
<style>{{ css|safe }}</style></head>
<body>
<div class="layout">
  <aside class="sidebar">
    <div class="sb-brand"><div class="logo"></div>MT5<b>//</b>TERM</div>
    <div class="sb-sub">trading terminal</div>
    <div class="sb-section">Workspace</div>
    <a class="sb-item" href="/"><span class="ic">\u25c8</span>Dashboard</a>
    <a class="sb-item" href="/panel"><span class="ic">\u25a3</span>Trading Panel</a>
    <a class="sb-item" href="/scheduled"><span class="ic">\u25c7</span>Scheduled Trades</a>
    <a class="sb-item on" href="/factory"><span class="ic">\u25c8</span>Factory</a>
    <div class="sb-foot">SPOTDUMP EA \u00b7 v2</div>
  </aside>
  <main class="main">
    <header class="topbar">
      <div>
        <div class="page-title">FACTORY</div>
        <div class="page-sub">forex news globe \u00b7 LED ticker \u00b7 local canvas render \u00b7 live ms/us</div>
      </div>
      <div class="spacer"></div>
      <div class="status-pill" id="sysPill"><span class="dot" id="sysdot"></span>
        <span id="sysLbl"></span></div>
      <div id="clock" class="mono"></div>
    </header>
    <div class="wrap">
      <div class="factory-grid">
        <section class="panel">
          <div class="sect"><h4>LED Ticker</h4>
            <div class="right"><span class="fsrc" id="clockTz">LOCAL</span></div></div>
          <div class="clockbox"><canvas id="clockCv"></canvas></div>
          <div class="sect"><h4>News Globe</h4>
            <div class="right"><span class="fsrc" id="mapSrc">loading map\u2026</span></div></div>
          <div class="globebox" id="globeBox">
            <canvas id="globeCv"></canvas>
            <div class="fhover" id="hoverBanner">\u2014 hover globe \u2014</div>
            <div class="fcoord" id="hoverCoord"></div>
            <div class="fhud"><div><b id="fps">0</b> fps</div>
              <div><b id="lat">0</b> ms frame</div>
              <div><b id="dotsN">0</b> dots</div>
              <div id="zoomLbl">x1.00</div></div>
          </div>
          <div class="sect"><h4>News Log</h4>
            <div class="right"><span class="fsrc" id="newsSrc">loading feed\u2026</span>
              <button class="btn mini" onclick="refreshNews(true)">REFRESH</button></div></div>
          <div class="tblwrap" id="newsTbl"><div class="empty">loading\u2026</div></div>
        </section>
        <section class="panel">
          <div class="sect"><h4>Next Release</h4></div>
          <div class="body">
            <div class="fnext" id="nextCd">--:--:--</div>
            <div id="nextInfo" class="mut" style="font-size:11px">waiting for feed\u2026</div>
          </div>
          <div class="sect"><h4>States On Alert</h4></div>
          <div class="body falert" id="alertBox">\u2014</div>
          <div class="sect"><h4>Controls</h4></div>
          <div class="body">
            <div class="fctrl">
              <button class="btn" id="tzBtn" onclick="toggleTz()">UTC</button>
              <button class="btn" id="h12Btn" onclick="toggle12h()">12H</button>
              <button class="btn on" id="autoBtn" onclick="toggleAuto()">PAUSE</button>
              <button class="btn on" id="highBtn" onclick="toggleHigh()">HIGH ONLY</button>
            </div>
            <div class="fctrl">
              <button class="btn" onclick="zoomBy(1.25)">Z+</button>
              <button class="btn" onclick="zoomBy(0.8)">Z-</button>
              <button class="btn" onclick="zoomReset()">1:1</button>
            </div>
            <div class="fsrc" style="margin-top:8px">drag = spin \u00b7 wheel = zoom \u00b7 double-click = 1:1</div>
          </div>
          <div class="sect"><h4>Hover</h4></div>
          <div class="body fhoverpanel" id="hoverPanel">\u2014 hover globe \u2014</div>
          <div class="sect"><h4>Feed</h4></div>
          <div class="body"><div class="fsrc" id="feedMeta">\u2014</div></div>
        </section>
      </div>
    </div>
  </main>
</div>
<div class="toast" id="toast"></div>
<script>{{ js|safe }}</script>
</body></html>"""

FACTORY_JS = COMMON_JS + """
/* ================= factory HUD: local, low-latency canvas render ========= */
const FC = {lon0:-40, lat0:22, zoom:1, auto:true, utc:false, use12h:false,
 highOnly:true, lastDrag:0, lastGlobe:0, dragging:false, dragX:0, dragY:0,
 hoverLatLon:null, hoverIso:null, hoverCcy:null, hoverName:null,
 fps:0, latMs:0, lastHud:0};
let FMAP=null, FEV=[], FSRC='loading', FAGE=null, FLOADING=false, FERR='';
let ALERTS={}, NEXT=null, ALERT_IDX=new Set(), ALERT_ISO=new Set();
let GIMG=null, CAM={sinLat0:0,cosLat0:1,lon0:0};
const DEG=Math.PI/180;
const RGB_N=[], RGB_A=[], RGB_H=[];
const SEG7={0:'abcdef',1:'bc',2:'abged',3:'abgcd',4:'fgbc',5:'afgcd',
 6:'afgedc',7:'abc',8:'abcdefg',9:'abcfgd','-':'g',' ':'',':':''};

function col32(r,g,b){return ((255<<24)|(b<<16)|(g<<8)|r)>>>0}
function buildPal(out,a,b){
 for(let i=0;i<16;i++){
  const t=i/15;
  out.push([Math.round(a[0]+(b[0]-a[0])*t),
   Math.round(a[1]+(b[1]-a[1])*t),Math.round(a[2]+(b[2]-a[2])*t)])}}
buildPal(RGB_N,[40,40,40],[178,178,178]);
buildPal(RGB_A,[122,58,58],[255,154,154]);
buildPal(RGB_H,[50,90,160],[170,205,255]);
const PAL_N=RGB_N.map(function(c){return col32(c[0],c[1],c[2])});
const PAL_A=RGB_A.map(function(c){return col32(c[0],c[1],c[2])});
const PAL_H=RGB_H.map(function(c){return col32(c[0],c[1],c[2])});

function pad2(n){return String(n).padStart(2,'0')}
function pad3(n){return String(n).padStart(3,'0')}
function b64buf(s){
 const bin=atob(s),u=new Uint8Array(bin.length);
 for(let i=0;i<bin.length;i++)u[i]=bin.charCodeAt(i);
 return u.buffer}

/* ---------- map payload once; trig precomputed for every dot ---------- */
function decodeMap(j){
 const lat=new Int16Array(b64buf(j.dots.lat));
 const lon=new Int16Array(b64buf(j.dots.lon));
 const ccy=new Int8Array(b64buf(j.dots.ccy));
 const n=lat.length;
 const sinLat=new Float32Array(n),cosLat=new Float32Array(n);
 const sinLon=new Float32Array(n),cosLon=new Float32Array(n);
 for(let i=0;i<n;i++){
  const la=lat[i]/100*DEG,lo=lon[i]/100*DEG;
  sinLat[i]=Math.sin(la);cosLat[i]=Math.cos(la);
  sinLon[i]=Math.sin(lo);cosLon[i]=Math.cos(lo)}
 const rings={},byIso={};
 const borders=j.borders.map(function(b){
  const a=new Int16Array(b64buf(b.p));
  (rings[b.i]=rings[b.i]||[]).push(a);
  return {i:b.i,a:a}});
 for(const c of j.countries)byIso[c.i]=c;
 return {lat:lat,lon:lon,ccy:ccy,sinLat:sinLat,cosLat:cosLat,
  sinLon:sinLon,cosLon:cosLon,n:n,borders:borders,rings:rings,
  byIso:byIso,ccyList:j.ccy,iso2ccy:j.iso2ccy,centers:j.centers,
  extra:j.extra,countries:j.countries,source:j.source}};

async function loadMap(){
 try{
  const j=await api('/api/factory/map');
  FMAP=decodeMap(j);
  $id('mapSrc').textContent=FMAP.source+' \u00b7 '+Math.round(FMAP.n/1000)+'k dots';
  $id('dotsN').textContent=FMAP.n.toLocaleString();
  const pill=$id('sysPill');if(pill)pill.className='status-pill';
  $id('sysLbl').textContent='';
 }catch(e){
  $id('mapSrc').textContent='map unavailable: '+e.message;
  $id('hoverBanner').textContent='factory map unavailable - '+e.message;
  const pill=$id('sysPill');if(pill)pill.className='status-pill bad';
  $id('sysLbl').textContent='HUD OFFLINE';}}

/* ---------- news + alert windows (same math as factory.py) ---------- */
function minsTo(iso){return (new Date(iso).getTime()-Date.now())/60000}
function alertsNow(){
 const al={};
 for(const e of FEV){
  if(FC.highOnly && e.impact!=='High')continue;
  const m=minsTo(e.dt);
  if(m>=-30 && m<=48*60){
   const a=al[e.currency]||(al[e.currency]={count:0,next_min:1e18,events:[]});
   a.count++;a.next_min=Math.min(a.next_min,m);a.events.push(e)}}
 for(const c in al)al[c].events.sort(function(a,b){
  return new Date(a.dt)-new Date(b.dt)});
 return al}
function nextEvent(){
 let best=null;
 for(const e of FEV){
  if(FC.highOnly && e.impact!=='High')continue;
  if(minsTo(e.dt)>=-5 && (!best || new Date(e.dt)<new Date(best.dt)))best=e}
 return best}
function recomputeAlerts(){
 ALERTS=alertsNow();NEXT=nextEvent();
 ALERT_IDX=new Set();ALERT_ISO=new Set();
 if(!FMAP)return;
 for(const c in ALERTS){
  const i=FMAP.ccyList.indexOf(c);
  if(i>=0)ALERT_IDX.add(i)}
 for(const iso in FMAP.iso2ccy){
  if(ALERTS[FMAP.iso2ccy[iso]])ALERT_ISO.add(iso)}}
function fmtCountdown(m){
 if(m>=-5 && m<=5)return 'LIVE';
 if(m<0){const x=Math.round(-m);
  return x<60?x+'m ago':Math.floor(x/60)+'h '+(x%60)+'m ago'}
 const s=Math.max(0,m*60);
 const hh=Math.floor(s/3600),mm=Math.floor(s%3600/60),ss=Math.floor(s%60);
 if(hh)return 'in '+hh+'h '+pad2(mm)+'m';
 return 'in '+mm+'m '+pad2(ss)+'s'}

async function refreshNews(force){
 if(force)$id('newsSrc').textContent='refreshing\u2026';
 try{
  const j=await api('/api/factory/news'+(force?'?force=1':''));
  FEV=j.events||[];FSRC=j.source||'feed';FAGE=j.age_s;
  FLOADING=!!j.loading;FERR=j.error||'';
  recomputeAlerts();renderNewsMeta();renderLog();
 }catch(e){FERR=e.message;renderNewsMeta()}}
function renderNewsMeta(){
 const bits=[];
 if(FLOADING)bits.push('fetching\u2026');
 bits.push(FSRC||'feed');
 if(FAGE!==null && FAGE!==undefined)bits.push('updated '+Math.round(FAGE)+'s ago');
 if(FERR)bits.push('ERR '+FERR);
 const txt=bits.join(' \u00b7 ');
 const el=$id('newsSrc');if(el){el.textContent=txt;el.title=txt}
 const fm=$id('feedMeta');
 if(fm)fm.textContent=(FEV.length?FEV.length+' events known':'no events')+' \u00b7 '+txt}

/* ---------- 7-segment LED clock (same geometry as factory.SevenSeg) ------ */
function hpoly(x,y,L,T){const h=T/2;
 return [[x+h,y],[x+L-h,y],[x+L,y+h],[x+L-h,y+T],[x+h,y+T],[x,y+h]]}
function vpoly(x,y,L,T){const h=T/2;
 return [[x+h,y],[x+T,y+h],[x+T,y+L-h],[x+h,y+L],[x,y+L-h],[x,y+h]]}
function polyPath(ctx,p){
 ctx.beginPath();ctx.moveTo(p[0][0],p[0][1]);
 for(let i=1;i<p.length;i++)ctx.lineTo(p[i][0],p[i][1]);
 ctx.closePath()}
function segDigit(ctx,x,y,w,h,ch){
 const T=Math.max(3,w*0.24),pad=T*0.7,vlen=h/2-pad*1.2,H=w-2*pad;
 const segs={a:hpoly(x+pad,y,H,T),
  g:hpoly(x+pad,y+h/2-T/2,H,T),
  d:hpoly(x+pad,y+h-T,H,T),
  f:vpoly(x,y+pad,vlen,T),b:vpoly(x+w-T,y+pad,vlen,T),
  e:vpoly(x,y+h/2+pad*0.2,vlen,T),
  c:vpoly(x+w-T,y+h/2+pad*0.2,vlen,T)};
 const on=SEG7[ch]||'';
 for(const k in segs){
  polyPath(ctx,segs[k]);
  if(on.indexOf(k)>=0){
   ctx.save();ctx.fillStyle='#ffffff';ctx.shadowColor='#b4b4b4';
   ctx.shadowBlur=9;ctx.fill();ctx.restore();
  }else{ctx.fillStyle='#1c1c1c';ctx.fill()}}
 return w+T*0.55}
function segNumber(ctx,x,y,w,h,text,blink,subsec){
 let cx=x;
 for(const ch of String(text)){
  if(ch===':'){cx+=segColon(ctx,cx,y,w*0.5,h,!blink||(subsec%1000)<600)}
  else cx+=segDigit(ctx,cx,y,w,h,ch)}
 return cx-x}
function segColon(ctx,x,y,w,h,on){
 const T=Math.max(3,w*0.24);
 for(const cy of [y+h*0.30,y+h*0.66]){
  ctx.fillStyle=on?'#ffffff':'#1c1c1c';
  ctx.fillRect(x,cy,T,T)}
 return T+w*0.3}
function tzName(d){
 try{
  const p=new Intl.DateTimeFormat('en-GB',{timeZoneName:'short'})
   .formatToParts(d);
  for(const x of p)if(x.type==='timeZoneName')return x.value
 }catch(e){}
 return 'LOCAL'}
function drawClock(){
 const cv=$id('clockCv');if(!cv)return;
 const W=cv.clientWidth||1000,H=cv.clientHeight||206;
 if(!W||!H)return;
 const dpr=Math.min(window.devicePixelRatio||1,2);
 if(cv.width!==Math.round(W*dpr)||cv.height!==Math.round(H*dpr)){
  cv.width=Math.round(W*dpr);cv.height=Math.round(H*dpr)}
 const ctx=cv.getContext('2d');
 ctx.setTransform(1,0,0,1,0,0);
 ctx.clearRect(0,0,cv.width,cv.height);
 ctx.fillStyle='#000000';ctx.fillRect(0,0,cv.width,cv.height);
 const sc=Math.min(W/1000,H/206);
 ctx.setTransform(dpr*sc,0,0,dpr*sc,
  (W-1000*sc)*dpr/2,(H-206*sc)*dpr/2);
 ctx.strokeStyle='#070707';ctx.lineWidth=1;
 for(let gx=0;gx<1000;gx+=44){ctx.beginPath();
  ctx.moveTo(gx,0);ctx.lineTo(gx,206);ctx.stroke()}
 const t=performance.timeOrigin+performance.now();
 const d=new Date(t);
 const ms=Math.floor(t)%1000,us=Math.floor(t*1000)%1000;
 const utc=FC.utc;
 let hr=utc?d.getUTCHours():d.getHours();
 const mi=utc?d.getUTCMinutes():d.getMinutes();
 const se=utc?d.getUTCSeconds():d.getSeconds();
 let ampm='';
 if(FC.use12h){ampm=hr<12?' AM':' PM';hr=hr%12||12}
 const hrTxt=(!FC.use12h||hr>=10)?pad2(hr):' '+hr;
 const x0=26,y0=12,bw=60,bh=104;
 let x=x0;
 x+=segNumber(ctx,x,y0,bw,bh,hrTxt,false,ms);
 x+=8;
 x+=segColon(ctx,x,y0,bw*0.5,bh,(ms<600));
 x+=8;
 x+=segNumber(ctx,x,y0,bw,bh,pad2(mi),false,ms);
 const sx=x+2*(bw+16)+22;
 segNumber(ctx,sx,y0+6,27,46,pad2(se),false,ms);
 ctx.textAlign='left';ctx.textBaseline='alphabetic';
 ctx.fillStyle='#ffffff';ctx.font='bold 13px "Courier New",monospace';
 ctx.fillText(pad3(ms)+' '+pad3(us),sx+4,y0+64);
 ctx.fillStyle='#8f8f8f';ctx.font='bold 9px "Courier New",monospace';
 ctx.fillText('MS   US',sx+4,y0+86);
 if(ampm){ctx.fillStyle='#ffffff';ctx.font='bold 26px "Courier New",monospace';
  ctx.textAlign='right';ctx.fillText(ampm,1000-30,34)}
 else{ctx.fillStyle='#8f8f8f';ctx.font='bold 12px "Courier New",monospace';
  ctx.textAlign='right';ctx.fillText('HR  MN',1000-30,34)}
 const months=['JANUARY','FEBRUARY','MARCH','APRIL','MAY','JUNE','JULY',
  'AUGUST','SEPTEMBER','OCTOBER','NOVEMBER','DECEMBER'];
 const wdays=['SUNDAY','MONDAY','TUESDAY','WEDNESDAY','THURSDAY','FRIDAY','SATURDAY'];
 const day=utc?d.getUTCDate():d.getDate();
 const mon=utc?d.getUTCMonth():d.getMonth();
 const year=utc?d.getUTCFullYear():d.getFullYear();
 const wd=wdays[utc?d.getUTCDay():d.getDay()];
 const tzn=utc?'UTC':tzName(d);
 ctx.textAlign='left';ctx.fillStyle='#ffffff';
 ctx.font='bold 17px "Courier New",monospace';
 ctx.fillText(months[mon]+' '+pad2(day)+' '+year+'   '+wd+'   '+tzn,30,148);
 ctx.fillStyle='#8f8f8f';ctx.font='bold 10px "Courier New",monospace';
 ctx.fillText('HR : MN : SC : MS : US',30,176)}

/* ---------- globe ---------- */
let PX=0,PY=0,PC=0;
function proj(lat,lon){
 const la=lat*DEG,dlo=lon*DEG-CAM.lon0;
 const sla=Math.sin(la),cla=Math.cos(la);
 const cdl=Math.cos(dlo),sdl=Math.sin(dlo);
 PC=CAM.sinLat0*sla+CAM.cosLat0*cla*cdl;
 PX=cla*sdl;PY=CAM.cosLat0*sla-CAM.sinLat0*cla*cdl;
 return PC}
function ringHit(a,lon,lat){
 /* points are interleaved [lat, lon] in hundredths of a degree */
 let inside=false;
 const n=a.length;
 for(let i=0,j=n-2;i<n;j=i,i+=2){
  const lati=a[i]/100,loni=a[i+1]/100;
  const latj=a[j]/100,lonj=a[j+1]/100;
  if(((lati>lat)!==(latj>lat)) &&
     (lon<(lonj-loni)*(lat-lati)/((latj-lati)||1e-12)+loni))inside=!inside}
 return inside}
function countryAt(lat,lon){
 let best=null,bestArea=1e18;
 if(!FMAP)return null;
 for(const c of FMAP.countries){
  const b=c.b;
  if(lon<b[0]-0.4||lon>b[2]+0.4||lat<b[1]-0.4||lat>b[3]+0.4)continue;
  const rings=FMAP.rings[c.i];
  if(!rings)continue;
  let hit=false;
  for(const a of rings)if(ringHit(a,lon,lat))hit=!hit;
  if(hit){
   const ar=Math.max(0.05,b[2]-b[0])*Math.max(0.05,b[3]-b[1]);
   if(ar<bestArea){bestArea=ar;best=c}}}
 if(best)return best;
 let nd=0.8,nc=null;
 for(const c of FMAP.countries){
  const dx=(c.c[0]-lon)*Math.cos(lat*DEG),dy=c.c[1]-lat;
  const dd=Math.sqrt(dx*dx+dy*dy);
  if(dd<nd){nd=dd;nc=c}}
 return nc}
function angDist(lat1,lon1,lat2,lon2){
 const s1=Math.sin(lat1*DEG),s2=Math.sin(lat2*DEG);
 const c1=Math.cos(lat1*DEG),c2=Math.cos(lat2*DEG);
 const c=Math.max(-1,Math.min(1,s1*s2+c1*c2*Math.cos((lon1-lon2)*DEG)));
 return Math.acos(c)/DEG}
function ccyAt(lat,lon){
 if(!FMAP)return null;
 let best=null,bd=1e18;
 for(const c in FMAP.centers){
  const v=FMAP.centers[c],dd=angDist(lat,lon,v[0],v[1]);
  if(dd<=v[2]&&dd<bd){best=c;bd=dd}}
 for(const c in FMAP.extra){
  for(const v of FMAP.extra[c]){
   const dd=angDist(lat,lon,v[0],v[1]);
   if(dd<=v[2]&&dd<bd){best=c;bd=dd}}}
 return best}

function drawGlobe(ts){
 const cv=$id('globeCv');if(!cv)return;
 const W=cv.clientWidth,H=cv.clientHeight;
 if(!W||!H)return;
 const dpr=Math.min(window.devicePixelRatio||1,2);
 /* Globe raster is capped at 1.25x: a 2x full-canvas ImageData fill +
    putImageData costs ~37 ms/frame, which made the globe stutter.  The
    clock still renders at the full dpr; the dotted globe is soft only
    at the sub-pixel level and stays 60 fps. */
 const rdpr=Math.min(dpr,1.25);
 if(cv.width!==Math.round(W*rdpr)||cv.height!==Math.round(H*rdpr)){
  cv.width=Math.round(W*rdpr);cv.height=Math.round(H*rdpr);GIMG=null}
 const ctx=cv.getContext('2d');
 const S=Math.min(W,H),R=(S/2-26)*FC.zoom;
 const cx=W/2,cy=H/2;
 const dtG=Math.min(0.15,(ts-(FC.lastGlobe||ts))/1000);
 FC.lastGlobe=ts;
 if(FC.auto && ts-FC.lastDrag>3000)FC.lon0=(FC.lon0+6.1*dtG)%360;
 CAM.sinLat0=Math.sin(FC.lat0*DEG);CAM.cosLat0=Math.cos(FC.lat0*DEG);
 CAM.lon0=FC.lon0*DEG;
 const imgW=cv.width,imgH=cv.height;
 if(!GIMG||GIMG.width!==imgW||GIMG.height!==imgH)
  GIMG=ctx.createImageData(imgW,imgH);
 const u32=new Uint32Array(GIMG.data.buffer);
 u32.fill(0xff000000);
 /* Grid belongs to the globe's square, not the full wide canvas:
    painting it edge-to-edge made the circular globe look like a
    separate overlay floating on a rectangular map.  factory.py draws
    its grid on the square SxS render surface, so match that. */
 const gsq=Math.round(S*rdpr);
 const gx0=Math.round((imgW-gsq)/2), gy0=Math.round((imgH-gsq)/2);
 const gx1=gx0+gsq, gy1=gy0+gsq;
 const gstep=Math.max(4,Math.round(40*rdpr));
 for(let gx=gx0;gx<=gx1;gx+=gstep){
  for(let y=gy0;y<gy1;y++)u32[y*imgW+gx]=0xff0b0b0b}
 for(let gy=gy0;gy<=gy1;gy+=gstep){
  const o=gy*imgW;
  for(let x=gx0;x<gx1;x++)u32[o+x]=0xff0b0b0b}
 if(FMAP && FMAP.n){
  /* Camera basis: sda/cda rotate by LON0, cc/x/y tilt by LAT0.
     Using sin/cos(lat0) for the longitude terms (the old bug) put the
     dotted map on a different angle than the border mesh and hover. */
  const sinp0=CAM.sinLat0,cosp0=CAM.cosLat0;
  const sinl0=Math.sin(CAM.lon0),cosl0=Math.cos(CAM.lon0);
  const sl=FMAP.sinLat,cl=FMAP.cosLat;
  const so=FMAP.sinLon,co=FMAP.cosLon,ci=FMAP.ccy,n=FMAP.n;
  const hoverIdx=(FC.hoverCcy?FMAP.ccyList.indexOf(FC.hoverCcy):-1);
  const hi=(hoverIdx===undefined)?-1:hoverIdx;
  const immIdx=(NEXT&&NEXT.currency)?FMAP.ccyList.indexOf(NEXT.currency):-1;
  const alertCcy=new Uint8Array(FMAP.ccyList.length);
  for(const ix of ALERT_IDX)if(ix>=0&&ix<alertCcy.length)alertCcy[ix]=1;
  /* 5.3x-dense lattice (~200k dots): normal dots are a single device
     pixel (2 device px once the render scale passes 1.2x), alerts/hover
     get one extra pixel.  Colours come straight from the u32 palettes so
     the inner loop stays tight enough at this density; the old per-dot
     size/colour maths would be too slow. */
  const blink=0.75+0.25*Math.sin(performance.now()/1000*2);
  const immG=Math.round(120*blink+60),immB=immG;
  const immPal=new Uint32Array(16);
  for(let d=0;d<16;d++){
   const t=d/15,r=RGB_A[d];
   immPal[d]=col32(Math.round(r[0]+(255-r[0])*t),
    Math.round(r[1]+(immG-r[1])*t),
    Math.round(r[2]+(immB-r[2])*t))}
  const psize=rdpr>=1.2?2:1;
  for(let i=0;i<n;i++){
   const sda=so[i]*cosl0-co[i]*sinl0;
   const cda=co[i]*cosl0+so[i]*sinl0;
   const cc=sinp0*sl[i]+cosp0*cl[i]*cda;
   if(cc<=0.03)continue;
   const x=cl[i]*sda;
   const y=cosp0*sl[i]-sinp0*cl[i]*cda;
   const px=((cx+x*R)*rdpr)|0,py=((cy-y*R)*rdpr)|0;
   const ps=psize+(((ci[i]>=0)&&(alertCcy[ci[i]]===1||ci[i]===hi))?1:0);
   if(px<1||py<1||px+ps>=imgW||py+ps>=imgH)continue;
   const d=Math.min(15,Math.max(0,((cc-0.03)/0.97*15.999)|0));
   const id=ci[i];
   let col=PAL_N[d];
   if(id>=0){
    if(alertCcy[id]===1)col=(id===immIdx)?immPal[d]:PAL_A[d];
    else if(id===hi)col=PAL_H[d]}
   const o=py*imgW+px;
   u32[o]=col;
   if(ps>1){u32[o+1]=col;u32[o+imgW]=col;u32[o+imgW+1]=col;
    if(ps>2){u32[o+2]=col;u32[o+imgW+2]=col;
     u32[o+2*imgW]=col;u32[o+2*imgW+1]=col;u32[o+2*imgW+2]=col}}}}
 ctx.putImageData(GIMG,0,0);
 ctx.setTransform(rdpr,0,0,rdpr,0,0);
 /* graticule */
 const grat=new Path2D();
 for(let lat=-60;lat<=60;lat+=30){
  let pen=false;
  for(let k=0;k<=96;k++){
   const c=proj(lat,-180+k*3.75);
   if(c>0.02){const px=cx+PX*R,py=cy-PY*R;
    if(!pen){grat.moveTo(px,py);pen=true}else grat.lineTo(px,py)}
   else pen=false}}
 for(let lon=-180;lon<180;lon+=30){
  let pen=false;
  for(let k=0;k<=96;k++){
   const c=proj(-90+k*1.875,lon);
   if(c>0.02){const px=cx+PX*R,py=cy-PY*R;
    if(!pen){grat.moveTo(px,py);pen=true}else grat.lineTo(px,py)}
   else pen=false}}
 ctx.strokeStyle='#2e2e2e';ctx.lineWidth=1;ctx.stroke(grat);
 /* Natural Earth borders, batched by colour */
 if(FMAP){
  const paths={base:new Path2D(),alert:new Path2D(),hover:new Path2D()};
  const hiIso=FC.hoverIso;
  for(const b of FMAP.borders){
   let key='base';
   if(b.i&&b.i===hiIso)key='hover';
   else if(ALERT_ISO.has(b.i))key='alert';
   const p=paths[key],a=b.a;
   let pen=false;
   for(let k=0;k<a.length;k+=2){
    const c=proj(a[k]/100,a[k+1]/100);
    if(c>0.02){const px=cx+PX*R,py=cy-PY*R;
     if(!pen){p.moveTo(px,py);pen=true}else p.lineTo(px,py)}
    else pen=false}}
  ctx.lineWidth=1;
  ctx.strokeStyle='#696969';ctx.stroke(paths.base);
  ctx.strokeStyle='#d28282';ctx.stroke(paths.alert);
  ctx.strokeStyle='#93c5fd';ctx.stroke(paths.hover)}
 ctx.strokeStyle='#5a5a5a';ctx.lineWidth=1;
 ctx.beginPath();ctx.arc(cx,cy,R,0,Math.PI*2);ctx.stroke();
 /* alert spikes + cross-links (same geometry as factory.py) */
 const imm=NEXT?NEXT.currency:null;
 for(const cur in ALERTS){
  if(!FMAP)break;
  const cc=FMAP.centers[cur];if(!cc)continue;
  if(proj(cc[0],cc[1])<=0.05)continue;
  const x=cx+PX*R,y=cy-PY*R;
  const dx=x-cx,dy=y-cy,n=Math.hypot(dx,dy)||1;
  const ux=dx/n,uy=dy/n;
  const L=Math.min(64,12+7*ALERTS[cur].count);
  const isNext=cur===imm;
  ctx.strokeStyle=isNext?'#ff2a2a':'#ffa726';
  ctx.lineWidth=isNext?3:2;
  ctx.beginPath();ctx.moveTo(x,y);ctx.lineTo(x+ux*L,y+uy*L);ctx.stroke();
  ctx.fillStyle=ctx.strokeStyle;
  const tip=isNext?3:2;
  ctx.beginPath();ctx.arc(x+ux*L,y+uy*L,tip,0,Math.PI*2);ctx.fill()}
 if(FMAP){
  const tops=[];
  for(const c in ALERTS)if(FMAP.centers[c])tops.push(c);
  if(tops.length>4)tops.length=4;
  for(let i=0;i<tops.length;i++)for(let j=i+1;j<tops.length;j++){
   const a=FMAP.centers[tops[i]],b=FMAP.centers[tops[j]];
   const dlo=((b[1]-a[1]+540)%360)-180;
   const pts=[];
   for(let k=0;k<=32;k++){
    const t=k/32;
    const la=a[0]+(b[0]-a[0])*t+Math.sin(t*Math.PI)*12;
    const lo=a[1]+dlo*t;
    const c=proj(la,lo);
    pts.push({x:cx+PX*R,y:cy-PY*R,v:c})}
   ctx.strokeStyle='#787878';ctx.lineWidth=1;ctx.beginPath();
   for(let k=0;k<pts.length-1;k+=2){
    if(pts[k].v>0.05&&pts[k+1].v>0.05){
     ctx.moveTo(pts[k].x,pts[k].y);ctx.lineTo(pts[k+1].x,pts[k+1].y)}}
   ctx.stroke()}}
 /* hover ring + crosshair */
 if(FMAP&&FC.hoverCcy&&FMAP.centers[FC.hoverCcy]){
  const cc=FMAP.centers[FC.hoverCcy];
  if(proj(cc[0],cc[1])>0.03){
   const rr=14+2*Math.sin(performance.now()/1000*2);
   ctx.strokeStyle='#93c5fd';ctx.lineWidth=2;
   ctx.beginPath();ctx.arc(cx+PX*R,cy-PY*R,rr,0,Math.PI*2);ctx.stroke();
   ctx.fillStyle='#93c5fd';
   ctx.beginPath();ctx.arc(cx+PX*R,cy-PY*R,3,0,Math.PI*2);ctx.fill()}}
 if(FC.hoverLatLon&&proj(FC.hoverLatLon[0],FC.hoverLatLon[1])>0){
  const hx=cx+PX*R,hy=cy-PY*R;
  ctx.strokeStyle='#93c5fd';ctx.lineWidth=1;ctx.beginPath();
  ctx.moveTo(hx-8,hy);ctx.lineTo(hx+8,hy);
  ctx.moveTo(hx,hy-8);ctx.lineTo(hx,hy+8);ctx.stroke()}}

/* ---------- hover / interaction ---------- */
function pointerGeo(e){
 const cv=$id('globeCv'),r=cv.getBoundingClientRect();
 const S=Math.min(r.width,r.height),R=(S/2-26)*FC.zoom;
 if(!R)return null;
 const x=(e.clientX-r.left-r.width/2)/R;
 const y=(r.height/2-(e.clientY-r.top))/R;
 const rho=Math.hypot(x,y);
 if(rho>1)return null;
 const lat0=FC.lat0*DEG,lon0=FC.lon0*DEG;
 let lat,lon;
 if(rho<1e-9){lat=lat0;lon=lon0}
 else{
  const c=Math.asin(Math.min(1,rho));
  lat=Math.asin(Math.cos(c)*Math.sin(lat0)+
   (y*Math.sin(c)*Math.cos(lat0))/rho);
  lon=lon0+Math.atan2(x*Math.sin(c),
   rho*Math.cos(c)*Math.cos(lat0)-y*Math.sin(c)*Math.sin(lat0))}
 let dLat=lat/DEG,dLon=lon/DEG;
 while(dLon<-180)dLon+=360;
 while(dLon>180)dLon-=360;
 return [dLat,dLon]}
function setHover(lat,lon){
 FC.hoverLatLon=[lat,lon];
 const c=countryAt(lat,lon);
 const cur=ccyAt(lat,lon);
 FC.hoverIso=c?c.i:null;
 FC.hoverName=c?c.n:null;
 FC.hoverCcy=cur||(c&&FMAP.iso2ccy[c.i])||null;
 let banner='\u25cb '+lat.toFixed(1)+', '+lon.toFixed(1);
 let panel='<div class="iso">POINT</div>'+
  '<div>'+lat.toFixed(2)+', '+lon.toFixed(2)+'</div>';
 if(c){
  const iso=c.i?' ('+c.i+')':'';
  const tag=FC.hoverCcy?'\u25b6 '+FC.hoverCcy+' ':'\u25cf ';
  banner=tag+c.n.toUpperCase()+iso;
  panel='<div class="iso">COUNTRY</div><div style="color:#fff;font-weight:600">'+
   esc(c.n)+iso+'</div>';
  if(FC.hoverCcy)panel+='<div class="iso" style="margin-top:6px">STATE</div>'+
   '<div style="color:#9ec5ff">'+esc(FC.hoverCcy)+'</div>';
  panel+='<div class="iso" style="margin-top:6px">COORD</div><div>'+
   lat.toFixed(2)+', '+lon.toFixed(2)+'</div>'}
 else panel+='<div class="iso" style="margin-top:6px">OCEAN / NO STATE</div>';
 $id('hoverBanner').textContent=banner;
 $id('hoverBanner').className='fhover'+(c?' hit':'');
 $id('hoverPanel').innerHTML=panel;
 $id('hoverCoord').textContent='LON '+lon.toFixed(1)+'  LAT '+lat.toFixed(1)}
function clearHover(){
 FC.hoverLatLon=null;FC.hoverIso=null;FC.hoverCcy=null;FC.hoverName=null;
 $id('hoverBanner').textContent='\u2014 hover globe \u2014';
 $id('hoverBanner').className='fhover';
 $id('hoverPanel').textContent='\u2014 hover globe \u2014';
 $id('hoverCoord').textContent=''}
function initGlobe(){
 const cv=$id('globeCv'),box=$id('globeBox');
 if(!cv)return;
 cv.addEventListener('pointerdown',function(e){
  FC.dragging=true;FC.dragX=e.clientX;FC.dragY=e.clientY;
  FC.lastDrag=performance.now();box.classList.add('dragging');
  try{cv.setPointerCapture(e.pointerId)}catch(err){}});
 cv.addEventListener('pointermove',function(e){
  if(FC.dragging){
   const dx=e.clientX-FC.dragX,dy=e.clientY-FC.dragY;
   FC.dragX=e.clientX;FC.dragY=e.clientY;
   FC.lon0=(FC.lon0-dx*0.45)%360;
   FC.lat0=Math.max(-60,Math.min(60,FC.lat0+dy*0.3));
   FC.lastDrag=performance.now()}
  const g=pointerGeo(e);
  if(g)setHover(g[0],g[1]);else clearHover()});
 function endDrag(e){
  FC.dragging=false;FC.lastDrag=performance.now();
  box.classList.remove('dragging');
  try{cv.releasePointerCapture(e.pointerId)}catch(err){}}
 cv.addEventListener('pointerup',endDrag);
 cv.addEventListener('pointercancel',endDrag);
 cv.addEventListener('pointerleave',function(){
  if(!FC.dragging)clearHover()});
 cv.addEventListener('wheel',function(e){
  e.preventDefault();zoomBy(e.deltaY>0?0.9:1.1)},{passive:false});
 cv.addEventListener('dblclick',zoomReset)}
function zoomBy(f){FC.zoom=Math.max(0.5,Math.min(5,FC.zoom*f));
 $id('zoomLbl').textContent='x'+FC.zoom.toFixed(2)}
function zoomReset(){FC.zoom=1;$id('zoomLbl').textContent='x1.00'}
function toggleTz(){FC.utc=!FC.utc;
 const b=$id('tzBtn');b.textContent=FC.utc?'LOCAL':'UTC';
 b.className='btn'+(FC.utc?' on':'');
 $id('clockTz').textContent=FC.utc?'UTC':'LOCAL';
 renderLog()}
function toggle12h(){FC.use12h=!FC.use12h;
 const b=$id('h12Btn');b.textContent=FC.use12h?'24H':'12H';
 b.className='btn'+(FC.use12h?' on':'')}
function toggleAuto(){FC.auto=!FC.auto;FC.lastDrag=performance.now();
 const b=$id('autoBtn');b.textContent=FC.auto?'PAUSE':'RESUME';
 b.className='btn'+(FC.auto?' on':'')}
function toggleHigh(){FC.highOnly=!FC.highOnly;
 const b=$id('highBtn');b.textContent=FC.highOnly?'HIGH ONLY':'ALL IMPACTS';
 b.className='btn'+(FC.highOnly?' on':'');
 recomputeAlerts();renderAlerts();renderLog()}

/* ---------- side panel + log ---------- */
function renderNext(){
 const el=$id('nextCd'),info=$id('nextInfo');
 if(!NEXT){el.textContent='--:--:--';el.className='fnext';
  info.textContent='no events in the 48 h window';return}
 const m=minsTo(NEXT.dt),s=Math.max(0,Math.floor(m*60));
 const hh=Math.floor(s/3600),mm=Math.floor(s%3600/60),ss=s%60;
 el.textContent=hh+':'+pad2(mm)+':'+pad2(ss);
 el.className='fnext'+((m>=-5&&m<=5)?' live':'');
 info.innerHTML='<b>'+esc(NEXT.currency)+'</b> \u00b7 '+esc(NEXT.title)+
  '<br><span class="'+(m>=-5&&m<=5?'down':'mut')+'">'+fmtCountdown(m)+
  '</span> \u00b7 fcast '+esc(NEXT.forecast)+' \u00b7 prev '+esc(NEXT.previous)+
  '<br><span class="mut">'+fmtEventTime(NEXT.dt)+'</span>'}
function renderAlerts(){
 const el=$id('alertBox');if(!el)return;
 const keys=[];
 for(const c in ALERTS)if(FMAP&&FMAP.centers[c])keys.push(c);
 keys.sort(function(a,b){return ALERTS[a].next_min-ALERTS[b].next_min});
 if(!keys.length){el.innerHTML='<span class="mut">no high-impact states in the next 48 h</span>';return}
 el.innerHTML=keys.map(function(c){
  const a=ALERTS[c],nm=(FMAP.centers[c]&&FMAP.centers[c][3])||c;
  return '<div><b>'+esc(c)+'</b> '+esc(nm)+
   ' <span class="mut">'+a.count+' event'+(a.count>1?'s':'')+' \u00b7 '+
   fmtCountdown(a.next_min)+'</span></div>'}).join('')}
function fmtEventTime(iso){
 const d=new Date(iso);
 if(isNaN(d.getTime()))return String(iso||'');
 if(FC.utc)return pad2(d.getUTCHours())+':'+pad2(d.getUTCMinutes())+' UTC';
 return d.toLocaleTimeString('en-GB',{hour:'2-digit',minute:'2-digit'})}
function fmtEventDate(iso){
 const d=new Date(iso);
 if(isNaN(d.getTime()))return '';
 if(FC.utc)return pad2(d.getUTCDate())+'/'+pad2(d.getUTCMonth()+1);
 return d.toLocaleDateString('en-GB',{day:'2-digit',month:'short'})}
function renderLog(){
 const el=$id('newsTbl');if(!el)return;
 const rows=[];
 for(const e of FEV){
  if(FC.highOnly&&e.impact!=='High')continue;
  const m=minsTo(e.dt);
  if(m>=-120&&m<=7*24*60)rows.push({m:m,e:e})}
 rows.sort(function(a,b){return a.m-b.m});
 if(rows.length>60)rows.length=60;
 if(!rows.length){el.innerHTML='<div class="empty">no events in window \u00b7 press REFRESH</div>';return}
 el.innerHTML='<table class="flog"><tr><th>in</th><th>time</th><th>cur</th>'+
  '<th>event</th><th>fcast</th><th>prev</th></tr>'+
  rows.map(function(r){
   const e=r.e,live=(r.m>=-5&&r.m<=5);
   return '<tr'+(live?' class="frow-live"':'')+'><td class="mono">'+
    (live?'LIVE':fmtCountdown(r.m))+'</td><td class="mono mut">'+
    fmtEventDate(e.dt)+' '+fmtEventTime(e.dt)+'</td><td class="mono">'+esc(e.currency)+
    '</td><td>'+esc(e.title)+'</td><td class="mono mut">'+esc(e.forecast)+
    '</td><td class="mono mut">'+esc(e.previous)+'</td></tr>'}).join('')+
  '</table>'}

/* ---------- frame loop ---------- */
function updateHud(){
 const f=$id('fps'),l=$id('lat');
 if(f)f.textContent=Math.round(FC.fps);
 if(l)l.textContent=FC.latMs.toFixed(1);
 $id('zoomLbl').textContent='x'+FC.zoom.toFixed(2)}
let LAST_TS=0;
function frame(ts){
 const t0=performance.now();
 const dt=Math.max(1,ts-(LAST_TS||ts));
 LAST_TS=ts;
 drawClock();
 if(ts-FC.lastGlobe>=33)drawGlobe(ts);
 const cost=performance.now()-t0;
 FC.latMs=FC.latMs?FC.latMs*0.88+cost*0.12:cost;
 FC.fps=FC.fps?FC.fps*0.9+(1000/dt)*0.1:1000/dt;
 if(ts-FC.lastHud>400){FC.lastHud=ts;updateHud()}
 requestAnimationFrame(frame)}
function fastTick(){
 recomputeAlerts();renderNext();renderAlerts();
 if(Math.floor(Date.now()/1000)%2===0)renderLog()}

loadMap();
initGlobe();
refreshNews();
setInterval(refreshNews,15000);
setInterval(fastTick,500);
requestAnimationFrame(frame);
"""


def render_factory() -> str:
    return render_template_string(FACTORY_TMPL, css=CSS, js=FACTORY_JS)
