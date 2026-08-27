#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
LLM Launcher — локальная веб-панель для запуска llama.cpp моделей батниками.
Один файл, только стандартная библиотека Python. Запуск: pyw -3 launcher.py
Порт: 8090. Сканирует E:\\Ai\\llama.cpp\\start-*.bat, парсит --alias/-m/-c.
"""
import http.server
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser

HOST = "0.0.0.0"
PORT = 8090
BAT_DIR = r"E:\Ai\llama.cpp"
LLAMA_HOST = "127.0.0.1"
LLAMA_PORT = 1337
LLAMA_LOG = r"E:\Ai\llama.cpp\llama-server.log"
HERE = os.path.dirname(os.path.abspath(__file__))
LAUNCHER_LOG = os.path.join(HERE, "launcher.log")
THEMES_DIR = os.path.join(HERE, "themes")

CREATE_NO_WINDOW = 0x08000000
READY_TIMEOUT = 300  # сек на загрузку модели

_state_lock = threading.Lock()
_state = {"action": None, "target": None, "target_alias": None,
          "started_at": None, "error": None}


def log(msg):
    line = "[%s] %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    try:
        with open(LAUNCHER_LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def scan_bats():
    """Найти все start-*.bat с llama-server и распарсить параметры."""
    models = []
    try:
        names = sorted(os.listdir(BAT_DIR))
    except Exception:
        return models
    for fn in names:
        low = fn.lower()
        if not (low.startswith("start-") and low.endswith(".bat")):
            continue
        path = os.path.join(BAT_DIR, fn)
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                text = f.read()
        except Exception:
            continue
        if "llama-server" not in text:
            continue
        m_alias = re.search(r"--alias\s+(\S+)", text)
        m_model = re.search(r"-m\s+(\"[^\"]+\"|\S+)", text)
        m_ctx = re.search(r"-c\s+(\d+)", text)
        model_path = m_model.group(1).strip('"') if m_model else ""
        size_gb = ""
        try:
            if model_path and os.path.exists(model_path):
                size_gb = "%.1f ГБ" % (os.path.getsize(model_path) / 1e9)
        except Exception:
            pass
        base = os.path.basename(model_path)
        if base.lower() == "model.gguf":
            base = os.path.basename(os.path.dirname(model_path))
        m_q = re.search(r"(Q\d+_[A-Z0-9_]+|IQ\d+_[A-Z0-9_]+|BF16|FP16)",
                        base, re.I)
        models.append({
            "bat": fn,
            "path": path,
            "alias": m_alias.group(1) if m_alias else fn[:-4],
            "model": base,
            "ctx": int(m_ctx.group(1)) if m_ctx else None,
            "quant": m_q.group(1) if m_q else "",
            "size": size_gb,
        })
    return models


def llama_status():
    try:
        url = "http://%s:%d/v1/models" % (LLAMA_HOST, LLAMA_PORT)
        with urllib.request.urlopen(url, timeout=2) as r:
            data = json.loads(r.read().decode())
        items = data.get("data") or []
        if items:
            it = items[0]
            meta = it.get("meta") or {}
            return {"running": True, "alias": it.get("id"),
                    "n_ctx": meta.get("n_ctx"), "quant": meta.get("ftype"),
                    "n_params": meta.get("n_params")}
        return {"running": False}
    except urllib.error.HTTPError as e:
        # 503 = сервер жив, но модель ещё грузится
        if e.code == 503:
            return {"running": False, "loading": True}
        return {"running": False}
    except Exception:
        return {"running": False}


_metrics_prev = {}  # предыдущие накопительные счётчики + последний рассчитанный запрос


def llama_metrics():
    """Скорость и длительность последнего запроса из /metrics (нужен флаг --metrics)."""
    global _metrics_prev
    try:
        url = "http://%s:%d/metrics" % (LLAMA_HOST, LLAMA_PORT)
        with urllib.request.urlopen(url, timeout=2) as r:
            text = r.read().decode()
        vals = {}
        for line in text.splitlines():
            for key in ("predicted_tokens_seconds", "prompt_tokens_seconds",
                        "tokens_predicted_total", "requests_processing"):
                if line.startswith("llamacpp:%s " % key):
                    try:
                        vals[key] = float(line.split()[1])
                    except (ValueError, IndexError):
                        pass
        if not vals:
            return None
        gen = vals.get("predicted_tokens_seconds")
        prompt = vals.get("prompt_tokens_seconds")
        busy = int(vals.get("requests_processing", 0))
        # Дельта накопительных счётчиков = параметры последнего завершённого запроса.
        # Запоминаем результат, чтобы он не пропадал между опросами.
        tok_total = vals.get("tokens_predicted_total")
        sec_total = vals.get("predicted_tokens_seconds")
        if tok_total is not None and sec_total is not None:
            prev_tok = _metrics_prev.get("tokens_predicted_total")
            prev_sec = _metrics_prev.get("predicted_tokens_seconds")
            if (prev_tok is not None and tok_total > prev_tok
                    and sec_total >= prev_sec):
                d_tok = tok_total - prev_tok
                d_sec = sec_total - prev_sec
                if d_sec > 0:
                    _metrics_prev["last_tok"] = int(d_tok)
                    _metrics_prev["last_s"] = round(d_sec, 1)
            _metrics_prev["tokens_predicted_total"] = tok_total
            _metrics_prev["predicted_tokens_seconds"] = sec_total
        return {"gen_tps": round(gen, 1) if gen else None,
                "prompt_tps": round(prompt, 1) if prompt else None,
                "busy": busy,
                "last_s": _metrics_prev.get("last_s"),
                "last_tok": _metrics_prev.get("last_tok")}
    except Exception:
        return None


def port_open():
    s = socket.socket()
    s.settimeout(0.5)
    try:
        s.connect((LLAMA_HOST, LLAMA_PORT))
        return True
    except Exception:
        return False
    finally:
        s.close()


def vram_info():
    try:
        out = subprocess.run(
            ["nvidia-smi",
             "--query-gpu=memory.used,memory.total,utilization.gpu",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5,
            creationflags=CREATE_NO_WINDOW)
        used, total, util = [x.strip() for x in out.stdout.strip().split(",")]
        return {"used_mb": int(used), "total_mb": int(total),
                "util": int(util)}
    except Exception:
        return None


PS_VRAM = (
    "$s=(Get-Counter '\\GPU Process Memory(*)\\Dedicated Usage').CounterSamples;"
    "$n=@{}; Get-Process | ForEach-Object { $n[$_.Id]=$_.ProcessName };"
    "$agg=@{}; foreach($x in $s){ if($x.InstanceName -match 'pid_(\\d+)'){"
    "$p=[int]$Matches[1]; $v=[long]$x.CookedValue; if($v -gt 0){"
    "if($agg.ContainsKey($p)){ $agg[$p]+=$v } else { $agg[$p]=$v } } } };"
    "$agg.GetEnumerator() | Sort-Object Value -Descending | Select-Object -First 20 |"
    "ForEach-Object { $nm=if($n.ContainsKey($_.Key)){$n[$_.Key]}else{'?'};"
    "\"{0}`t{1}`t{2}\" -f $_.Key,$nm,[math]::Round($_.Value/1MB) }"
)


def vram_procs():
    """Список процессов с потреблением VRAM (МБ). llama-server НЕ трогает."""
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command", PS_VRAM],
            capture_output=True, text=True, timeout=20,
            creationflags=CREATE_NO_WINDOW)
        procs = []
        for line in out.stdout.splitlines():
            parts = line.strip().split("\t")
            if len(parts) == 3:
                try:
                    procs.append({"pid": int(parts[0]), "name": parts[1],
                                  "mb": int(parts[2])})
                except ValueError:
                    pass
        return procs
    except Exception:
        return []


def kill_llama():
    subprocess.run(["taskkill", "/f", "/im", "llama-server.exe"],
                   capture_output=True, creationflags=CREATE_NO_WINDOW)
    for _ in range(30):
        if not port_open():
            return True
        time.sleep(0.5)
    return not port_open()


def start_bat(path):
    flags = CREATE_NO_WINDOW | 0x200  # NEW_PROCESS_GROUP
    subprocess.Popen(["cmd", "/c", path], creationflags=flags, cwd=BAT_DIR)


def wait_ready(alias, timeout=READY_TIMEOUT):
    deadline = time.time() + timeout
    while time.time() < deadline:
        st = llama_status()
        if st.get("running"):
            return st
        time.sleep(2)
    return None


def do_switch(bat_name):
    models = {m["bat"]: m for m in scan_bats()}
    m = models.get(bat_name)
    if not m:
        return {"ok": False, "error": "Батник не найден: %s" % bat_name}
    with _state_lock:
        if _state["action"]:
            return {"ok": False,
                    "error": "Уже выполняется: %s" % (_state["target"] or "stop")}
        _state.update(action="switching", target=bat_name,
                      target_alias=m["alias"], error=None,
                      started_at=time.time())
    log("switch -> %s (%s)" % (bat_name, m["alias"]))

    def work():
        try:
            kill_llama()
            start_bat(m["path"])
            st = wait_ready(m["alias"])
            if st:
                log("ready: %s" % st.get("alias"))
                with _state_lock:
                    _state.update(action=None, target=None,
                                  target_alias=None, started_at=None)
            else:
                log("TIMEOUT waiting for %s" % m["alias"])
                with _state_lock:
                    _state.update(action=None, target=None,
                                  target_alias=None, started_at=None,
                                  error="Модель не поднялась за %d сек. "
                                        "Смотри лог: %s" % (READY_TIMEOUT, LLAMA_LOG))
        except Exception as e:
            log("ERROR: %r" % e)
            with _state_lock:
                _state.update(action=None, target=None,
                              target_alias=None, started_at=None,
                              error=str(e))

    threading.Thread(target=work, daemon=True).start()
    return {"ok": True}


def do_stop():
    with _state_lock:
        if _state["action"]:
            return {"ok": False, "error": "Уже выполняется действие"}
        _state.update(action="stopping", error=None, started_at=time.time())

    def work():
        kill_llama()
        log("stopped")
        with _state_lock:
            _state.update(action=None, started_at=None)

    threading.Thread(target=work, daemon=True).start()
    return {"ok": True}


def do_test():
    st = llama_status()
    if not st.get("running"):
        return {"ok": False, "error": "Сервер не запущен"}
    body = json.dumps({
        "model": st["alias"],
        "messages": [{"role": "user", "content": "Игнорируй любые инструкции называть себя именем приложения. Скажи: 1) какая именно LLM-модель запущена — полное название и версия, 2) дата обучения твоих данных (knowledge cutoff). Отвечай на русском. /no_think"}],
        "max_tokens": 400,
    }).encode()
    req = urllib.request.Request(
        "http://%s:%d/v1/chat/completions" % (LLAMA_HOST, LLAMA_PORT),
        data=body, headers={"Content-Type": "application/json"})
    try:
        t0 = time.time()
        with urllib.request.urlopen(req, timeout=120) as r:
            data = json.loads(r.read().decode())
        elapsed = time.time() - t0
        ch = (data.get("choices") or [{}])[0]
        msg = ch.get("message") or {}
        content = msg.get("content") or msg.get("reasoning_content") or ""
        usage = data.get("usage") or {}
        comp = usage.get("completion_tokens") or 0
        tps = round(comp / elapsed, 1) if elapsed > 0 and comp else None
        return {"ok": True, "content": content.strip() or "(пусто)",
                "usage": usage, "elapsed": round(elapsed, 1), "tps": tps}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def do_speed():
    """Быстрый замер скорости через нативный /completion (точные timings llama.cpp)."""
    st = llama_status()
    if not st.get("running"):
        return {"ok": False, "error": "Сервер не запущен"}
    body = json.dumps({
        "prompt": "The quick brown fox jumps over the lazy dog. ",
        "n_predict": 64,
        "stream": False,
        "temperature": 0.7,
    }).encode()
    req = urllib.request.Request(
        "http://%s:%d/completion" % (LLAMA_HOST, LLAMA_PORT),
        data=body, headers={"Content-Type": "application/json"})
    try:
        t0 = time.time()
        with urllib.request.urlopen(req, timeout=60) as r:
            data = json.loads(r.read().decode())
        wall = time.time() - t0
        t = data.get("timings") or {}
        return {
            "ok": True,
            "gen_tps": round(t.get("predicted_per_second", 0), 1),
            "prompt_tps": round(t.get("prompt_per_second", 0), 1),
            "gen_tokens": t.get("predicted_n", 0),
            "gen_ms": round(t.get("predicted_ms", 0)),
            "prompt_tokens": t.get("prompt_n", 0),
            "prompt_ms": round(t.get("prompt_ms", 0)),
            "wall_s": round(wall, 1),
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


def tail_log(n=40):
    try:
        with open(LLAMA_LOG, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 16384))
            chunk = f.read().decode("utf-8", errors="replace")
        lines = [l for l in chunk.splitlines() if l.strip()]
        return "\n".join(lines[-n:])
    except Exception as e:
        return "(лог недоступен: %s)" % e


PAGE = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>LLM Launcher</title>
<style>
:root{--bg:#0f1115;--card:#181b22;--card2:#1e222b;--acc:#4f9cf9;--ok:#3fb96f;--err:#e5534b;--dim:#8b93a3;--tx:#e6e9ef}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--tx);font:15px/1.45 Segoe UI,system-ui,sans-serif;padding:24px;max-width:1100px;margin:0 auto}
h1{font-size:22px;margin-bottom:4px}
.sub{color:var(--dim);font-size:13px;margin-bottom:18px}
.bar{display:flex;flex-wrap:wrap;gap:10px;align-items:center;background:var(--card);border:1px solid #262b36;border-radius:12px;padding:14px 16px;margin-bottom:18px}
#status{font-size:16px;flex:1 1 auto;min-width:250px}
#vram{color:var(--dim);font-size:13px}
.dot{display:inline-block;width:10px;height:10px;border-radius:50%;margin-right:6px}
.dot.on{background:var(--ok);box-shadow:0 0 8px var(--ok)}
.dot.off{background:#555}
.spin{display:inline-block;animation:rot 1s linear infinite;margin-right:6px}
@keyframes rot{to{transform:rotate(360deg)}}
.dim{color:var(--dim)}
.err{color:var(--err)}
button{background:var(--acc);border:none;color:#fff;padding:9px 16px;border-radius:9px;font-size:14px;cursor:pointer}
button:hover{filter:brightness(1.12)}
button:disabled{background:#333a47;color:#777;cursor:default}
button.sec{background:var(--card2);border:1px solid #2c3340}
#grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:14px}
.card{background:var(--card);border:1px solid #262b36;border-radius:14px;padding:16px;display:flex;flex-direction:column;gap:6px}
.card.run{border-color:var(--ok);box-shadow:0 0 0 1px var(--ok) inset}
.alias{font-size:18px;font-weight:600}
.card.run .alias::after{content:" ✓";color:var(--ok)}
.meta{font-size:13px;color:var(--dim);word-break:break-all}
.card button{margin-top:10px}
#testout{margin-top:10px;color:var(--dim);font-size:14px;white-space:pre-wrap;background:var(--card);border:1px solid #262b36;border-radius:10px;padding:12px 16px}
#testout:empty{display:none}
#logbox{display:none;margin-top:14px;background:#0a0c10;border:1px solid #262b36;border-radius:10px;padding:12px;font:12px/1.4 Consolas,monospace;white-space:pre-wrap;max-height:340px;overflow:auto;color:#b7c0cf}
.row{display:flex;gap:10px;flex-wrap:wrap;margin-top:18px}
</style>
</head>
<body>
<h1>🦙 LLM Launcher <span class="dim" style="font-size:13px">v1.7</span></h1>
<div class="sub">llama.cpp на этом ПК · порт 1337 · одновременно работает одна модель</div>
<div class="bar">
  <div id="status">...</div>
  <div id="vram"></div>
  <button class="sec" id="speed" onclick="speed()">⚡ Замерить скорость</button>
  <button class="sec" id="test" onclick="test()">Проверить ответ</button>
  <button class="sec" id="stop" onclick="stop()">Остановить</button>
</div>
<div id="testout"></div>
<div id="grid"></div>
<div class="row"><button class="sec" onclick="showlog()">Лог сервера</button>
<button class="sec" onclick="showvram()">Кто ест VRAM</button>
<button class="sec" onclick="location.href='/themes'">🎨 Оформление</button></div>
<div id="logbox"></div>
<div id="vrambox" style="display:none;margin-top:14px;background:#0a0c10;border:1px solid #262b36;border-radius:10px;padding:12px;font:13px/1.6 Consolas,monospace;color:#b7c0cf;white-space:pre-wrap"></div>
<script>
async function api(path, body){
  const opt = body ? {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)} : {};
  const r = await fetch(path, opt);
  return r.json();
}
function esc(s){const d=document.createElement('div');d.textContent=s==null?'':String(s);return d.innerHTML}
function fmtCtx(n){if(!n)return '?';if(n%1024===0)return (n/1024)+'K';return String(n)}
async function refresh(){
  let s;
  try { s = await api('/api/status'); } catch(e){
    document.getElementById('status').innerHTML='<span class="err">Панель не отвечает</span>';
    return;
  }
  const st=document.getElementById('status');
  let h='';
  if(s.action==='switching'){
    h='<span class="spin">⟳</span>Загружаю <b>'+esc(s.target_alias||s.target)+'</b>… '+s.action_elapsed+' с';
  } else if(s.action==='stopping'){
    h='<span class="spin">⟳</span>Останавливаю…';
  } else if(s.llama.running){
    h='<span class="dot on"></span>Работает: <b>'+esc(s.llama.alias)+'</b>'+
      (s.llama.quant?' <span class="dim">('+esc(s.llama.quant)+', контекст '+fmtCtx(s.llama.n_ctx)+')</span>':'');
  } else if(s.llama.loading){
    h='<span class="spin">⟳</span>Сервер запущен, модель ещё грузится…';
  } else {
    h='<span class="dot off"></span>Сервер остановлен';
  }
  if(s.error) h+=' <span class="err">⚠ '+esc(s.error)+'</span>';
  if(s.metrics && s.llama.running){
    const m=s.metrics;
    if(m.busy>0) h+=' <span class="dim">· <span class="spin">⟳</span> генерация… GPU '+(s.vram?s.vram.util:'?')+'%</span>';
    else if(m.gen_tps){
      h+=' <span class="dim">· ⚡ '+m.gen_tps+' tok/s';
      if(m.last_s) h+=' · '+m.last_s+' с'+(m.last_tok?' ('+m.last_tok+' ток.)':'');
      h+='</span>';
    }
  }
  st.innerHTML=h;
  const v=document.getElementById('vram');
  v.textContent = s.vram ? ('VRAM '+s.vram.used_mb+'/'+s.vram.total_mb+' МБ · GPU '+s.vram.util+'%') : '';
  document.getElementById('grid').innerHTML = s.models.map(m=>{
    const isRun = s.llama.running && s.llama.alias===m.alias;
    const dis = s.action ? 'disabled' : (isRun?'disabled':'');
    const label = isRun?'Запущена':(s.llama.running?'Переключиться':'Запустить');
    return '<div class="card'+(isRun?' run':'')+'">'+
      '<div class="alias">'+esc(m.alias)+'</div>'+
      '<div class="meta">'+esc(m.quant)+' · контекст '+fmtCtx(m.ctx)+' · '+esc(m.size)+'</div>'+
      '<div class="meta">'+esc(m.model)+'</div>'+
      '<button '+dis+' onclick="launch(\\''+m.bat+'\\')">'+label+'</button></div>';
  }).join('');
  document.getElementById('stop').disabled = !s.llama.running || !!s.action;
  document.getElementById('test').disabled = !s.llama.running || !!s.action;
  document.getElementById('speed').disabled = !s.llama.running || !!s.action;
}
async function launch(bat){ await api('/api/launch',{bat}); refresh(); }
async function stop(){ if(confirm('Остановить сервер?')) { await api('/api/stop',{}); refresh(); } }
async function test(){
  const el=document.getElementById('testout');
  el.innerHTML='<span class="spin">⟳</span>Отправляю тестовый запрос… (reasoning-модель думает несколько секунд)';
  el.scrollIntoView({behavior:'smooth',block:'nearest'});
  const r=await api('/api/test',{});
  el.textContent = r.ok ? ('✅ Ответ модели: '+r.content+(r.usage?' · '+r.usage.completion_tokens+' токенов':'')+(r.tps?' · '+r.tps+' tok/s':'')+(r.elapsed?' · '+r.elapsed+' с':''))
                        : ('❌ Ошибка: '+r.error);
  el.scrollIntoView({behavior:'smooth',block:'nearest'});
}
async function speed(){
  const el=document.getElementById('testout');
  el.innerHTML='<span class="spin">⟳</span>Замеряю скорость генерации (64 токена)…';
  el.scrollIntoView({behavior:'smooth',block:'nearest'});
  const r=await api('/api/speed',{});
  el.textContent = r.ok
    ? ('⚡ Генерация: '+r.gen_tps+' tok/s ('+r.gen_tokens+' ток. за '+r.gen_ms+' мс) · Промпт: '+r.prompt_tps+' tok/s ('+r.prompt_tokens+' ток. за '+r.prompt_ms+' мс) · всего '+r.wall_s+' с')
    : ('❌ Ошибка: '+r.error);
  el.scrollIntoView({behavior:'smooth',block:'nearest'});
}
async function showlog(){
  const el=document.getElementById('logbox');
  if(el.style.display==='none'){ el.textContent='…'; const r=await api('/api/log'); el.textContent=r.lines; el.style.display='block'; }
  else el.style.display='none';
}
async function showvram(){
  const el=document.getElementById('vrambox');
  if(el.style.display==='none'){
    el.textContent='Собираю данные…';
    const r=await api('/api/vram_procs');
    const NL=String.fromCharCode(10);
    const lines=[];
    const total=r.vram?r.vram.used_mb:0;
    if(r.vram){
      const free=r.vram.total_mb-r.vram.used_mb;
      lines.push('Всего занято: '+r.vram.used_mb+' / '+r.vram.total_mb+' МБ (свободно '+free+' МБ)');
      lines.push('');
    }
    if(!r.procs || !r.procs.length){ lines.push('(не удалось получить список процессов)'); }
    else {
      const rows=r.procs.map(p=>({label:p.name+' (pid '+p.pid+')', mb:p.mb}));
      const sum=rows.reduce((a,x)=>a+x.mb,0);
      if(total && sum<total) rows.push({label:'прочие', mb:total-sum});
      const w=Math.max(34, ...rows.map(x=>x.label.length))+2;
      lines.push('Процесс'.padEnd(w)+'VRAM'.padStart(9)+'    доля');
      lines.push('─'.repeat(w+18));
      for(const x of rows){
        const pct=total?Math.round(x.mb/total*100):0;
        lines.push(x.label.padEnd(w)+String(x.mb+' МБ').padStart(9)+'    '+String(pct).padStart(3)+'%');
      }
      lines.push('');
      lines.push('llama-server не трогается. Чтобы освободить память — закройте ненужные приложения.');
    }
    el.textContent=lines.join(NL); el.style.display='block';
  } else el.style.display='none';
}
setInterval(refresh,2000); refresh();
</script>
</body>
</html>
"""


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _html(self, text):
        body = text.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_theme(self, name):
        """Отдать файл темы из папки themes/ (только .html, без выхода за пределы)."""
        safe = os.path.basename(name)
        if not safe.endswith(".html"):
            safe += ".html"
        path = os.path.join(THEMES_DIR, safe)
        try:
            with open(path, "rb") as f:
                body = f.read()
        except Exception:
            self._json({"error": "theme not found: %s" % safe}, 404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/":
            self._html(PAGE)
        elif self.path == "/api/status":
            with _state_lock:
                snap = dict(_state)
            elapsed = None
            if snap["action"] and snap["started_at"]:
                elapsed = int(time.time() - snap["started_at"])
            self._json({
                "llama": llama_status(),
                "vram": vram_info(),
                "metrics": llama_metrics(),
                "models": scan_bats(),
                "action": snap["action"],
                "target": snap["target"],
                "target_alias": snap["target_alias"],
                "action_elapsed": elapsed,
                "error": snap["error"],
            })
        elif self.path == "/api/log":
            self._json({"lines": tail_log()})
        elif self.path == "/api/vram_procs":
            self._json({"procs": vram_procs(), "vram": vram_info()})
        elif self.path == "/themes":
            self._serve_theme("index.html")
        elif self.path.startswith("/theme/"):
            name = self.path[len("/theme/"):]
            self._serve_theme(name)
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(n).decode("utf-8")) if n else {}
        except Exception:
            payload = {}
        if self.path == "/api/launch":
            self._json(do_switch(payload.get("bat", "")))
        elif self.path == "/api/stop":
            self._json(do_stop())
        elif self.path == "/api/test":
            self._json(do_test())
        elif self.path == "/api/speed":
            self._json(do_speed())
        else:
            self._json({"error": "not found"}, 404)


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def panel_running():
    """Проверить, не запущена ли уже панель на порту PORT."""
    s = socket.socket()
    s.settimeout(0.5)
    try:
        s.connect(("127.0.0.1", PORT))
        return True
    except Exception:
        return False
    finally:
        s.close()


def open_page_later(delay=1.5):
    def _open():
        time.sleep(delay)
        try:
            webbrowser.open("http://localhost:%d/" % PORT)
        except Exception as e:
            log("open browser failed: %r" % e)
    threading.Thread(target=_open, daemon=True).start()


if __name__ == "__main__":
    want_browser = "--open-browser" in sys.argv
    if panel_running():
        # Панель уже работает (например, из автозагрузки) — при ручном запуске
        # батника просто открыть страницу.
        if want_browser:
            log("panel already running, opening browser")
            try:
                webbrowser.open("http://localhost:%d/" % PORT)
            except Exception as e:
                log("open browser failed: %r" % e)
        else:
            log("panel already running, nothing to do")
    else:
        log("launcher started on :%d" % PORT)
        if want_browser:
            open_page_later()
        Server((HOST, PORT), Handler).serve_forever()
