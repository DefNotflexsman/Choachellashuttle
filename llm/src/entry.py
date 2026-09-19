import csv
import io
import json
import time
import traceback
import xml.etree.ElementTree as ET
from datetime import datetime
from urllib.parse import urlparse, parse_qs

from workers import WorkerEntrypoint, Response

# ─── In-memory log store (caps at 5000 entries) ───
_logs = []
_MAX_LOGS = 5000

# ─── Paths excluded from logging ───
_EXCLUDE_PATHS = {"/logs", "/logs/api"}

def add_log(level="info", source="http", message="", path=None, **extra):
    if path and path in _EXCLUDE_PATHS:
        return
    entry = {
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "level": level,
        "source": source,
        "message": message,
        **extra,
    }
    _logs.append(entry)
    if len(_logs) > _MAX_LOGS:
        _logs[:] = _logs[-_MAX_LOGS:]

# ─── /logs viewer page ───
LOGS_HTML = """<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Logs</title><style>
*{box-sizing:border-box;margin:0;padding:0}body{font-family:monospace;background:#0f172a;color:#e2e8f0;padding:1rem}
h1{color:#38bdf8;font-size:1.2rem;margin-bottom:1rem}
.bar{display:flex;gap:.5rem;margin-bottom:1rem;flex-wrap:wrap}
input,select{padding:.4rem .6rem;border-radius:6px;border:1px solid #475569;background:#1e293b;color:#e2e8f0;font-size:.85rem}
input{flex:1;min-width:200px}
.stats{display:flex;gap:1rem;margin-bottom:1rem;font-size:.85rem}
.stat{padding:.3rem .6rem;border-radius:6px;background:#1e293b}
table{width:100%;border-collapse:collapse;font-size:.8rem}
th{background:#334155;padding:.4rem;text-align:left;position:sticky;top:0}
td{padding:.35rem .4rem;border-top:1px solid #1e293b;vertical-align:top}
.info{color:#38bdf8}.success{color:#34d399}.warn{color:#fbbf24}.error{color:#f87171}
</style></head><body>
<h1>📋 Logs</h1>
<div class="stats" id="stats"></div>
<div class="bar">
<input id="q" placeholder="Search logs..." oninput="render()">
<select id="lvl" onchange="render()"><option value="">All levels</option><option>info</option><option>success</option><option>warn</option><option>error</option></select>
<select id="src" onchange="render()"><option value="">All sources</option><option>http</option><option>ai</option><option>auth</option><option>external</option><option>websocket</option><option>admin</option></select>
</div>
<table><thead><tr><th>Time</th><th>Level</th><th>Source</th><th>Method</th><th>Path</th><th>Status</th><th>Duration</th><th>Message</th></tr></thead>
<tbody id="rows"></tbody></table>
<script>
let logs=[];
async function load(){const r=await fetch('/logs/api');const d=await r.json();logs=d.logs||[];renderStats();render()}
function renderStats(){const c={info:0,success:0,warn:0,error:0};logs.forEach(l=>c[l.level]=(c[l.level]||0)+1);
document.getElementById('stats').innerHTML='<span class="stat info">Info: '+c.info+'</span><span class="stat success">Success: '+c.success+'</span><span class="stat warn">Warn: '+c.warn+'</span><span class="stat error">Error: '+c.error+'</span><span class="stat">Total: '+logs.length+'</span>'}
function render(){
  const q=document.getElementById('q').value.toLowerCase();
  const lvl=document.getElementById('lvl').value;
  const src=document.getElementById('src').value;
  const f=logs.filter(l=>(!lvl||l.level===lvl)&&(!src||l.source===src)&&JSON.stringify(l).toLowerCase().includes(q));
  const tbody=document.getElementById('rows');
  tbody.innerHTML='';
  for(const l of f.slice(0,500)){
    const tr=document.createElement('tr');
    const cells=[
      l.timestamp.slice(11,19),l.level,l.source,l.method||'',l.path||'',
      l.status||'',l.duration_ms?l.duration_ms+'ms':'',l.message
    ];
    for(let i=0;i<cells.length;i++){
      const td=document.createElement('td');
      td.textContent=cells[i];
      if(i===1)td.className=l.level;
      if(i===5&&l.status&&l.status>=400)td.className='error';
      tr.appendChild(td);
    }
    tbody.appendChild(tr);
  }
}
load();setInterval(load,5000);
</script></body></html>"""


class ResponseFormatter:
    MORSE_CODE_DICT = {
        'A': '.-', 'B': '-...', 'C': '-.-.', 'D': '-..', 'E': '.', 'F': '..-.',
        'G': '--.', 'H': '....', 'I': '..', 'J': '.---', 'K': '-.-', 'L': '.-..',
        'M': '--', 'N': '-.', 'O': '---', 'P': '.--.', 'Q': '--.-', 'R': '.-.',
        'S': '...', 'T': '-', 'U': '..-', 'V': '...-', 'W': '.--', 'X': '-..-',
        'Y': '-.--', 'Z': '--..', '1': '.----', '2': '..---', '3': '...--',
        '4': '....-', '5': '.....', '6': '-....', '7': '--...', '8': '---..',
        '9': '----.', '0': '-----', ' ': '/'
    }

    @classmethod
    def format(cls, prompt, response_text, fmt_type):
        fmt = (fmt_type or "text").lower()

        if fmt == "json":
            return json.dumps({"prompt": prompt, "response": response_text}, indent=2)
        elif fmt == "yaml":
            escaped_prompt = prompt.replace("\n", " ")
            escaped_response = response_text.replace("\n", "\n  ")
            return f'output:\n  prompt: "{escaped_prompt}"\n  response: |\n  {escaped_response}'
        elif fmt == "toml":
            escaped_prompt = prompt.replace('"', '\\"')
            escaped_response = response_text.replace('"', '\\"')
            return f'[result]\nprompt = "{escaped_prompt}"\nresponse = """\n{escaped_response}\n"""'
        elif fmt == "xml":
            root = ET.Element("output")
            p_elem = ET.SubElement(root, "prompt")
            p_elem.text = prompt
            r_elem = ET.SubElement(root, "response")
            r_elem.text = response_text
            return ET.tostring(root, encoding="unicode")
        elif fmt == "csv":
            output = io.StringIO()
            writer = csv.writer(output)
            writer.writerow(["prompt", "response"])
            writer.writerow([prompt, response_text])
            return output.getvalue().strip()
        elif fmt == "hex":
            combined = f"Prompt: {prompt}\nResponse: {response_text}"
            return combined.encode("utf-8").hex()
        elif fmt == "binary":
            combined = f"Prompt: {prompt}\nResponse: {response_text}"
            return " ".join(format(b, "08b") for b in combined.encode("utf-8"))
        elif fmt == "morse":
            combined = f"{prompt} {response_text}".upper()
            morse_words = []
            for char in combined:
                if char in cls.MORSE_CODE_DICT:
                    morse_words.append(cls.MORSE_CODE_DICT[char])
            return " ".join(morse_words)
        else:
            return f"Prompt: {prompt}\nResponse:\n{response_text}"


HTML_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>LLM Assistant</title>
<style>
  :root {
    --bg: #0f0f23;
    --card: #1a1a2e;
    --accent: #f67c1f;
    --accent-hover: #ff8c33;
    --text: #e0e0e0;
    --text-muted: #888;
    --border: #2a2a4a;
    --radius: 12px;
  }
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
    background: var(--bg);
    color: var(--text);
    min-height: 100vh;
    display: flex;
    align-items: center;
    justify-content: center;
    padding: 20px;
  }
  .container { width: 100%; max-width: 720px; }
  .card {
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 32px;
    box-shadow: 0 8px 32px rgba(0,0,0,0.4);
  }
  h1 {
    font-size: 1.8rem;
    margin-bottom: 4px;
    background: linear-gradient(135deg, var(--accent), #ff6b6b);
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
    background-clip: text;
  }
  .subtitle { color: var(--text-muted); font-size: 0.9rem; margin-bottom: 24px; }
  .field { margin-bottom: 16px; }
  label {
    display: block; font-size: 0.8rem; text-transform: uppercase;
    letter-spacing: 0.5px; color: var(--text-muted); margin-bottom: 6px;
  }
  textarea, select {
    width: 100%; background: #12121f; border: 1px solid var(--border);
    border-radius: 8px; padding: 12px; color: var(--text);
    font-size: 0.95rem; font-family: inherit; outline: none; transition: border-color 0.2s;
  }
  textarea { resize: vertical; min-height: 100px; }
  textarea:focus, select:focus { border-color: var(--accent); }
  .row { display: flex; gap: 12px; }
  .row .field { flex: 1; }
  button {
    width: 100%; background: var(--accent); color: #fff; border: none;
    border-radius: 8px; padding: 14px; font-size: 1rem; font-weight: 600;
    cursor: pointer; transition: background 0.2s;
  }
  button:hover { background: var(--accent-hover); }
  button:disabled { opacity: 0.5; cursor: not-allowed; }
  .output {
    margin-top: 24px; background: #12121f; border: 1px solid var(--border);
    border-radius: 8px; padding: 16px; min-height: 60px; white-space: pre-wrap;
    word-break: break-word; font-family: 'SF Mono', 'Fira Code', monospace;
    font-size: 0.85rem; line-height: 1.6; max-height: 400px; overflow-y: auto;
  }
  .output:empty::before { content: "Response will appear here..."; color: var(--text-muted); }
  .loading {
    display: inline-block; width: 16px; height: 16px;
    border: 2px solid var(--border); border-top-color: var(--accent);
    border-radius: 50%; animation: spin 0.6s linear infinite;
  }
  @keyframes spin { to { transform: rotate(360deg); } }
  .status { font-size: 0.8rem; color: var(--text-muted); margin-top: 8px; }
  .logs-link { text-align: center; margin-top: 16px; }
  .logs-link a { color: var(--text-muted); text-decoration: none; font-size: 0.8rem; }
  .logs-link a:hover { color: var(--accent); }
  .query-info {
    margin-top: 16px; padding: 12px; background: #12121f;
    border: 1px solid var(--border); border-radius: 8px;
    font-family: monospace; font-size: 0.8rem; color: #888;
    white-space: pre-wrap; word-break: break-all;
  }
  .query-warn { color: #fbbf24; }
</style>
</head>
<body>
<div class="container">
  <div class="card">
    <h1>LLM Assistant</h1>
    <p class="subtitle">Powered by Cloudflare Workers AI</p>
    <div class="field">
      <label for="prompt">Prompt</label>
      <textarea id="prompt" placeholder="Ask anything..."></textarea>
    </div>
    <div class="row">
      <div class="field">
        <label for="format">Output Format</label>
        <select id="format">
          <option value="text">Text</option>
          <option value="json">JSON</option>
          <option value="yaml">YAML</option>
          <option value="toml">TOML</option>
          <option value="xml">XML</option>
          <option value="csv">CSV</option>
          <option value="hex">Hex</option>
          <option value="binary">Binary</option>
          <option value="morse">Morse Code</option>
        </select>
      </div>
      <div class="field">
        <label for="model">Model</label>
        <select id="model">
          <option value="@cf/meta/llama-3.2-3b-instruct">Llama 3.2 3B</option>
          <option value="@cf/meta/llama-3.2-1b-instruct">Llama 3.2 1B</option>
          <option value="@cf/meta/llama-3.1-8b-instruct-fp8">Llama 3.1 8B FP8</option>
          <option value="@cf/zai-org/glm-4.7-flash">GLM 4.7 Flash</option>
          <option value="@cf/ibm-granite/granite-4.0-h-micro">Granite 4.0 Micro</option>
        </select>
      </div>
    </div>
    <button id="submit" onclick="sendRequest()">Generate</button>
    <div class="status" id="status"></div>
    <div class="output" id="output"></div>
    <div class="logs-link"><a href="/logs">📋 View Logs</a></div>
  </div>
</div>
<script>
async function sendRequest() {
  const prompt = document.getElementById('prompt').value.trim();
  const format = document.getElementById('format').value;
  const model = document.getElementById('model').value;
  const output = document.getElementById('output');
  const status = document.getElementById('status');
  const btn = document.getElementById('submit');

  if (!prompt) { output.textContent = 'Please enter a prompt.'; return; }

  btn.disabled = true;
  btn.innerHTML = '<span class="loading"></span> Generating...';
  output.textContent = '';
  status.textContent = '';

  try {
    const res = await fetch(window.location.pathname, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ prompt, format, model })
    });
    const text = await res.text();
    if (!res.ok) {
      output.textContent = 'Error: ' + text;
    } else {
      output.textContent = text;
    }
  } catch (e) {
    output.textContent = 'Request failed: ' + e.message;
  }

  btn.disabled = false;
  btn.textContent = 'Generate';
}
document.getElementById('prompt').addEventListener('keydown', (e) => {
  if (e.ctrlKey && e.key === 'Enter') sendRequest();
});

// ─── URL Query Parser ───
(function parseQuery() {
  const params = new URLSearchParams(window.location.search);
  const raw = window.location.search;
  if (!raw) return;

  const display = document.createElement('div');
  display.className = 'query-info';

  let parsed = 'Parsed URL Query:\\n';
  for (const [key, value] of params.entries()) {
    const hasHtml = /[<>]/.test(value);
    const hasScript = /<script/i.test(value);
    const hasTag = /<\\/?[a-z][\\s\\S]*>/i.test(value);

    parsed += '\\n  ' + key + ' = ' + value;
    if (hasHtml) parsed += '  \\u26a0\\ufe0f HTML detected';
    if (hasScript) parsed += '  \\u26a0\\ufe0f SCRIPT tag detected';
    if (hasTag) parsed += '  \\u26a0\\ufe0f HTML tag detected';
  }

  parsed += '\\n\\nRaw query string:\\n  ' + raw;

  display.textContent = parsed;
  const container = document.querySelector('.container');
  if (container) container.appendChild(display);

  const promptParam = params.get('prompt');
  if (promptParam) {
    const textarea = document.getElementById('prompt');
    if (textarea) textarea.value = promptParam;
  }

  const qParam = params.get('q');
  if (qParam && !promptParam) {
    const textarea = document.getElementById('prompt');
    if (textarea) textarea.value = qParam;
  }
})();
</script>
</body>
</html>"""


class Default(WorkerEntrypoint):
    async def fetch(self, request):
        url = urlparse(request.url)
        path = url.path
        method = request.method
        start = time.monotonic()

        # ─── /logs routes ───
        if path == "/logs" and method == "GET":
            duration = round((time.monotonic() - start) * 1000, 2)
            add_log(level="success", source="http", message=f"GET /logs → 200",
                    method="GET", path="/logs", status=200, duration_ms=duration)
            return Response(LOGS_HTML, headers={"Content-Type": "text/html; charset=utf-8"})

        if path == "/logs/api" and method == "GET":
            duration = round((time.monotonic() - start) * 1000, 2)
            add_log(level="success", source="http", message=f"GET /logs/api → 200",
                    method="GET", path="/logs/api", status=200, duration_ms=duration)
            return Response.json({"logs": list(reversed(_logs))})

        # ─── /health ───
        if path == "/health":
            duration = round((time.monotonic() - start) * 1000, 2)
            add_log(level="success", source="http", message=f"GET /health → 200",
                    method="GET", path="/health", status=200, duration_ms=duration)
            return Response.json({"ok": True})

        # ─── /api/v4/jsony ───
        if path == "/api/v4/jsony" and method == "GET":
            duration = round((time.monotonic() - start) * 1000, 2)
            add_log(level="success", source="http", message=f"GET /api/v4/jsony → 200",
                    method="GET", path="/api/v4/jsony", status=200, duration_ms=duration)
            return Response.json({
                "message": "json v4 minecraft starter code will go here",
                "server-version": "1.21.11",
                "type": "http",
                "website-type": "llm"
            })

        # ─── Serve HTML page for GET browser requests ───
        if method == "GET":
            duration = round((time.monotonic() - start) * 1000, 2)
            add_log(level="success", source="http", message=f"GET {path} → 200",
                    method="GET", path=path, status=200, duration_ms=duration)
            return Response(HTML_PAGE, headers={"Content-Type": "text/html; charset=utf-8"})

        # ─── API endpoint for POST (AI prompt) ───
        if method == "POST":
            try:
                body = await request.json()
            except Exception:
                duration = round((time.monotonic() - start) * 1000, 2)
                add_log(level="error", source="http", message=f"POST {path} → 400 Invalid JSON",
                        method="POST", path=path, status=400, duration_ms=duration)
                return Response.json({"error": "Invalid JSON body"}, status=400)

            prompt = body.get("prompt", "")
            fmt = body.get("format", "text")
            model = body.get("model", "@cf/meta/llama-3.2-3b-instruct")
            max_tokens = body.get("max_tokens", 256)

            if not prompt:
                duration = round((time.monotonic() - start) * 1000, 2)
                add_log(level="warn", source="http", message=f"POST {path} → 400 No prompt",
                        method="POST", path=path, status=400, duration_ms=duration)
                return Response.json({"error": "No prompt provided."}, status=400)

            # Log the AI prompt
            add_log(level="info", source="ai", message=f"Prompt received: {prompt[:200]}",
                    prompt=prompt, model=model)

            try:
                result = await self.env.AI.run(
                    model,
                    messages=[{"role": "user", "content": prompt}],
                    max_tokens=max_tokens,
                )
                if isinstance(result, dict) and "response" in result:
                    response_text = result["response"]
                elif isinstance(result, str):
                    response_text = result
                else:
                    response_text = json.dumps(result)
            except Exception as e:
                duration = round((time.monotonic() - start) * 1000, 2)
                add_log(level="error", source="ai", message=f"AI request failed: {e}",
                        prompt=prompt, model=model, error=str(e), duration_ms=duration)
                add_log(level="error", source="http", message=f"POST {path} → 502 AI error",
                        method="POST", path=path, status=502, duration_ms=duration)
                return Response.json({"error": f"Workers AI error: {e}"}, status=502)

            # Log the AI response
            ai_duration = round((time.monotonic() - start) * 1000, 2)
            add_log(level="success", source="ai", message=f"AI response ({ai_duration}ms): {response_text[:200]}",
                    response=response_text[:500], model=model, duration_ms=ai_duration)

            formatted = ResponseFormatter.format(prompt, response_text, fmt)

            duration = round((time.monotonic() - start) * 1000, 2)
            add_log(level="success", source="http", message=f"POST {path} → 200",
                    method="POST", path=path, status=200, duration_ms=duration)

            return Response(formatted, headers={"Content-Type": "text/plain; charset=utf-8"})

        # ─── Method not allowed ───
        duration = round((time.monotonic() - start) * 1000, 2)
        add_log(level="warn", source="http", message=f"{method} {path} → 405",
                method=method, path=path, status=405, duration_ms=duration)
        return Response.json({"error": "Method not allowed"}, status=405)