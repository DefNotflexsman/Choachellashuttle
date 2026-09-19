import os
import time
import json
import hashlib
import traceback
from datetime import datetime, timedelta
from typing import Optional, List

import httpx
from fastapi import (
    FastAPI, Request, Header, Query, Path, HTTPException,
    WebSocket, WebSocketDisconnect, Depends, Form, Cookie, status,
)
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from jose import jwt, JWTError
from pydantic import BaseModel
from workers import asgi

app = FastAPI(debug=True, title="Asynchronous Portal Engine", description="FastAPI on Cloudflare Workers")

# ─── In-memory log store (caps at 5000 entries) ───
_logs = []
_MAX_LOGS = 5000
# ─── Paths to exclude from logging ───
_EXCLUDE_PATHS = {"/logs", "/logs/api"}

def _should_log(path):
    """Return False if the path is in the exclude set."""
    return path not in _EXCLUDE_PATHS
def add_log(level="info", source="http", message="", path=None, **extra):
    # Skip logging for excluded paths
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
_EXCLUDE_PATHS = {"/logs", "/logs/api", "/health", "/favicon.ico"}

def add_log(level="info", source="http", message="", **extra):
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

# ─── HTTP middleware: logs every request + response ───
@app.middleware("http")
async def logging_middleware(request: Request, call_next):
    start = time.monotonic()
    method = request.method
    path = request.url.path
    ip = request.client.host if request.client else ""
    try:
        response = await call_next(request)
        duration = round((time.monotonic() - start) * 1000, 2)
        level = "success" if response.status_code < 400 else "warn" if response.status_code < 500 else "error"
        add_log(level=level, source="http", message=f"{method} {path} → {response.status_code}",
                method=method, path=path, status=response.status_code, ip=ip, duration_ms=duration)
        response.headers["X-Process-Time"] = str(time.time() - start)
        return response
    except Exception as e:
        duration = round((time.monotonic() - start) * 1000, 2)
        add_log(level="error", source="http", message=f"{method} {path} → 500 {e}",
                method=method, path=path, status=500, ip=ip, duration_ms=duration,
                error=traceback.format_exc())
        raise

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
document.getElementById('stats').innerHTML=`<span class="stat info">Info: ${c.info}</span><span class="stat success">Success: ${c.success}</span><span class="stat warn">Warn: ${c.warn}</span><span class="stat error">Error: ${c.error}</span><span class="stat">Total: ${logs.length}</span>`}
function render(){const q=document.getElementById('q').value.toLowerCase();const lvl=document.getElementById('lvl').value;const src=document.getElementById('src').value;
const f=logs.filter(l=>(!lvl||l.level===lvl)&&(!src||l.source===src)&&JSON.stringify(l).toLowerCase().includes(q));
document.getElementById('rows').innerHTML=f.slice(0,500).map(l=>`<tr><td>${l.timestamp.slice(11,19)}</td><td class="${l.level}">${l.level}</td><td>${l.source}</td><td>${l.method||''}</td><td>${l.path||''}</td><td class="${l.status&&l.status>=400?'error':''}">${l.status||''}</td><td>${l.duration_ms?l.duration_ms+'ms':''}</td><td>${l.message}</td></tr>`).join('')}
load();setInterval(load,5000);
</script></body></html>"""

@app.get("/logs", response_class=HTMLResponse)
async def logs_page():
    return LOGS_HTML

@app.get("/logs/api")
async def logs_api():
    return JSONResponse({"logs": list(reversed(_logs))})

# ─── Config ───
SECRET_KEY = os.environ.get("JWT_SECRET_KEY", "super-secret-jwt-key-change-in-production")
API_KEY = os.environ.get("API_KEY", "default_secret_api_key")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 30

def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()

def verify_password(plain_password: str, hashed_password: str) -> bool:
    return hash_password(plain_password) == hashed_password

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="token", auto_error=False)

fake_users_db = {
    "admin": {"username": "admin", "full_name": "Portal Admin", "hashed_password": hash_password("secret123"), "is_admin": True},
    "johndoe": {"username": "johndoe", "full_name": "John Doe", "hashed_password": hash_password("userpassword123"), "is_admin": False},
}
github_creds = {"info": {"user": "DefNotflexsman/server-att2"}}

class Token(BaseModel):
    access_token: str
    token_type: str

class User(BaseModel):
    username: str
    full_name: Optional[str] = None
    is_admin: bool = False

class ItemResponse(BaseModel):
    item_id: int
    name: str

class StatusResponse(BaseModel):
    status: str
    message: str

class UUIDResponse(BaseModel):
    status: str
    requested_amount: int
    uuids: List[str]

class ProcessResponse(BaseModel):
    status: str
    message: str
    pid: int
    initiated_by: Optional[str] = None

LANDING_PAGE_HTML = """<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Asynchronous Portal Engine</title>
<style>
:root{--bg:#0f172a;--card:#1e293b;--text:#f8fafc;--muted:#94a3b8;--accent:#38bdf8;--get:#0284c7;--post:#16a34a;--ws:#d97706;}
body{font-family:system-ui,sans-serif;background:var(--bg);color:var(--text);margin:0;padding:2rem;}
.container{max-width:900px;margin:0 auto;}
header{margin-bottom:2.5rem;border-bottom:1px solid #334155;padding-bottom:1rem;}
h1{color:var(--accent);margin-bottom:0.5rem;}
.section-title{color:var(--muted);font-size:0.85rem;text-transform:uppercase;letter-spacing:0.05em;margin:1.5rem 0 0.75rem;}
.route-card{background:var(--card);border-radius:8px;padding:1rem 1.25rem;margin-bottom:0.75rem;display:flex;align-items:center;gap:1rem;box-shadow:0 4px 6px -1px rgba(0,0,0,0.1);}
.badge{font-weight:bold;padding:0.25rem 0.65rem;border-radius:4px;text-transform:uppercase;font-size:0.75rem;min-width:45px;text-align:center;}
.badge.get{background:var(--get);}.badge.post{background:var(--post);}.badge.ws{background:var(--ws);}
.endpoint{font-family:monospace;font-size:1rem;flex-grow:1;}.description{color:var(--muted);font-size:0.9rem;}
</style></head><body><div class="container"><header><h1>Asynchronous Portal Engine</h1><p style="color:#94a3b8;margin:0;">Service API Route Directory</p></header><main>
<div class="section-title">General & UI</div>
<div class="route-card"><span class="badge get">GET</span><span class="endpoint">/</span><span class="description">API Directory</span></div>
<div class="route-card"><span class="badge get">GET</span><span class="endpoint">/logs</span><span class="description">Log Viewer</span></div>
<div class="route-card"><span class="badge get">GET</span><span class="endpoint">/admindashboard</span><span class="description">Control Panel</span></div>
<div class="route-card"><span class="badge get">GET</span><span class="endpoint">/docs</span><span class="description">Swagger UI</span></div>
<div class="section-title">Authentication</div>
<div class="route-card"><span class="badge post">POST</span><span class="endpoint">/token</span><span class="description">OAuth2 Token</span></div>
<div class="route-card"><span class="badge get">GET</span><span class="endpoint">/api/authentication</span><span class="description">Validate API Key</span></div>
<div class="section-title">Core APIs</div>
<div class="route-card"><span class="badge post">POST</span><span class="endpoint">/api/request</span><span class="description">Inspect Request</span></div>
<div class="route-card"><span class="badge get">GET</span><span class="endpoint">/api/items/{item_id}</span><span class="description">Get Item</span></div>
<div class="route-card"><span class="badge get">GET</span><span class="endpoint">/api/status</span><span class="description">Generate UUIDs</span></div>
<div class="section-title">Admin (Protected)</div>
<div class="route-card"><span class="badge get">GET</span><span class="endpoint">/api/admin/metrics</span><span class="description">Server Stats</span></div>
<div class="route-card"><span class="badge post">POST</span><span class="endpoint">/api/server/mc</span><span class="description">Launch Process</span></div>
<div class="section-title">WebSockets</div>
<div class="route-card"><span class="badge ws">WS</span><span class="endpoint">/ws</span><span class="description">WebSocket Gateway</span></div>
<div class="route-card"><span class="badge ws">WS</span><span class="endpoint">/server/accept</span><span class="description">Secondary WS</span></div>
</main></div></body></html>"""

ADMIN_LOGIN_HTML = """<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><title>Admin Login</title>
<style>body{font-family:system-ui,sans-serif;background:#0f172a;color:#f8fafc;display:flex;justify-content:center;align-items:center;height:100vh;margin:0;}
.card{background:#1e293b;padding:2rem;border-radius:8px;width:300px;box-shadow:0 4px 6px rgba(0,0,0,0.3);}
h2{margin-top:0;color:#38bdf8;}input{width:100%;padding:0.5rem;margin:0.5rem 0 1rem;border-radius:4px;border:1px solid #334155;background:#0f172a;color:#fff;}
button{width:100%;padding:0.6rem;background:#0284c7;color:white;border:none;border-radius:4px;font-weight:bold;cursor:pointer;}button:hover{background:#0369a1;}</style>
</head><body><div class="card"><h2>Admin Access</h2><form action="/admindashboard/login" method="POST"><label>Username</label><input type="text" name="username" required><label>Password</label><input type="password" name="password" required><button type="submit">Log In</button></form></div></body></html>"""

ADMIN_PANEL_HTML = """<!DOCTYPE html><html><head><title>Admin Dashboard</title></head><body style="font-family:system-ui,sans-serif;background:#0f172a;color:#fff;padding:40px;"><h1 style="color:#38bdf8;">FastAPI Control Panel</h1><p>Status: Authenticated as Admin.</p><a href="/admindashboard/logout" style="color:#ef4444;">Log Out</a></body></html>"""

def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    to_encode = data.copy()
    expire = datetime.utcnow() + (expires_delta or timedelta(minutes=15))
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)

async def get_current_user(token: str = Depends(oauth2_scheme)) -> User:
    credentials_exception = HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Could not validate credentials", headers={"WWW-Authenticate": "Bearer"})
    if not token:
        raise credentials_exception
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username = payload.get("sub")
        if username is None:
            raise credentials_exception
    except JWTError:
        raise credentials_exception
    user_dict = fake_users_db.get(username)
    if user_dict is None:
        raise credentials_exception
    return User(username=user_dict["username"], full_name=user_dict["full_name"], is_admin=user_dict["is_admin"])

async def verify_api_key(x_api_key: Optional[str] = Header(None)) -> str:
    if not x_api_key or x_api_key != API_KEY:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or missing API key header.")
    return x_api_key

class ItemNotFoundException(Exception):
    def __init__(self, item_id: int):
        self.item_id = item_id

@app.exception_handler(ItemNotFoundException)
async def item_not_found_handler(request: Request, exc: ItemNotFoundException):
    return JSONResponse(status_code=status.HTTP_404_NOT_FOUND, content={"message": f"Item with ID {exc.item_id} does not exist."})

@app.post("/api/request", tags=["API Parser"])
async def inspect_request_endpoint(request: Request):
    return {"status": "success", "parsed_request": {"path": request.url.path, "method": request.method, "client_ip": request.client.host if request.client else "Unknown", "headers": dict(request.headers), "query_params": dict(request.query_params), "timestamp": datetime.utcnow().isoformat()}}

@app.post("/token", response_model=Token, tags=["Auth"])
async def login_for_access_token(form_data: OAuth2PasswordRequestForm = Depends()):
    user_dict = fake_users_db.get(form_data.username)
    if not user_dict or not verify_password(form_data.password, user_dict["hashed_password"]):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Incorrect username or password", headers={"WWW-Authenticate": "Bearer"})
    access_token = create_access_token(data={"sub": user_dict["username"]}, expires_delta=timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES))
    return {"access_token": access_token, "token_type": "bearer"}

@app.get("/api/items/{item_id}", response_model=ItemResponse, tags=["Items"])
async def read_item(item_id: int = Path(..., ge=1)):
    if item_id > 100:
        raise ItemNotFoundException(item_id=item_id)
    return {"item_id": item_id, "name": f"Sample Item #{item_id}"}

@app.get("/api/status", response_model=UUIDResponse, tags=["External APIs"])
async def get_uuid_status(amount: int = Query(10, ge=1, le=1000)):
    async with httpx.AsyncClient() as client:
        try:
            response = await client.get(f"https://www.uuidtools.com/api/generate/v1/count/{amount}", timeout=10.0)
            if response.status_code != 200:
                raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Failed to retrieve UUIDs.")
            return {"status": "success", "requested_amount": amount, "uuids": response.json()}
        except httpx.RequestError as exc:
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=f"Network error: {exc}")

@app.get("/api/authentication", response_model=StatusResponse, tags=["Auth"])
async def get_auth_status(api_key: str = Depends(verify_api_key)):
    return {"status": "ok", "message": "Service authentication valid."}

@app.get("/api/endpoint/test", tags=["Testing"])
async def handle_api_get():
    return {"message": "Retrieved endpoint status via GET"}

@app.post("/api/endpoint/test", status_code=status.HTTP_201_CREATED, tags=["Testing"])
async def handle_api_post():
    return {"message": "Resource created via POST"}

@app.get("/api/admin/metrics", tags=["Admin"])
async def get_admin_statistics(current_user: User = Depends(get_current_user)):
    return {"status": "success", "requested_by": current_user.username, "metrics": {"uptime": "99.9%", "active_nodes": 4, "requests_processed": 1024}}

@app.post("/api/server/mc", response_model=ProcessResponse, tags=["Admin"])
async def launch_minecraft_server(current_user: User = Depends(get_current_user)):
    return {"status": "success", "message": "Background process initiated (simulated on Workers).", "pid": 0, "initiated_by": current_user.username}

@app.get("/api/github/info", tags=["External APIs"])
async def get_github_repo_info():
    repo = github_creds["info"]["user"]
    async with httpx.AsyncClient() as client:
        try:
            response = await client.get(f"https://api.github.com/repos/{repo}", headers={"User-Agent": "FastAPI-Portal-Engine"}, timeout=10.0)
            if response.status_code != 200:
                raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"GitHub API error (HTTP {response.status_code}).")
            return {"status": "success", "repository": repo, "data": response.json()}
        except httpx.RequestError as exc:
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=f"Network error: {exc}")

@app.get("/api/v4/jsony")
async def jsonyv4():
    return {"message": "json v4 minecraft starter code will go here", "server-version": "1.21.11", "type": "http", "website-type": "llm"}

@app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def home_endpoint():
    return HTMLResponse(content=LANDING_PAGE_HTML, status_code=200)

@app.get("/admindashboard", response_class=HTMLResponse, include_in_schema=False)
async def admin_dashboard_page(admin_session: Optional[str] = Cookie(None)):
    if admin_session == "authenticated":
        return HTMLResponse(content=ADMIN_PANEL_HTML, status_code=200)
    return HTMLResponse(content=ADMIN_LOGIN_HTML, status_code=200)

@app.post("/admindashboard/login", include_in_schema=False)
async def admin_login_submit(username: str = Form(...), password: str = Form(...)):
    user_dict = fake_users_db.get(username)
    if user_dict and verify_password(password, user_dict["hashed_password"]) and user_dict.get("is_admin"):
        response = RedirectResponse(url="/admindashboard", status_code=status.HTTP_303_SEE_OTHER)
        response.set_cookie(key="admin_session", value="authenticated", httponly=True)
        return response
    return RedirectResponse(url="https://llm-assistant.llmdev.workers.dev/", status_code=status.HTTP_303_SEE_OTHER)

@app.get("/admindashboard/logout", include_in_schema=False)
async def admin_logout():
    response = RedirectResponse(url="/admindashboard", status_code=status.HTTP_303_SEE_OTHER)
    response.delete_cookie("admin_session")
    return response

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    try:
        while True:
            data = await websocket.receive_text()
            await websocket.send_text(f"Server received: {data}")
    except WebSocketDisconnect:
        print("[WS] Client disconnected")

@app.websocket("/server/accept")
async def websocket_accept_endpoint(websocket: WebSocket):
    await websocket.accept()
    try:
        while True:
            data = await websocket.receive_text()
            await websocket.send_text(f"Server received: {data}")
    except WebSocketDisconnect:
        print("[WS] Client disconnected")

Default = asgi.entrypoint(app)