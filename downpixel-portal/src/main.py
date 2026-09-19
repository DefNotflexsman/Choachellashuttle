import os
import time
import hashlib
import json
import uuid as uuid_lib
from datetime import datetime, timedelta
from typing import Optional, List, Dict

from fastapi import (
    FastAPI, Request, Header, Query, Path, HTTPException,
    WebSocket, WebSocketDisconnect, Depends, Form, Cookie, status,
)
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from jose import jwt, JWTError
from pydantic import BaseModel

SECRET_KEY = os.environ.get("JWT_SECRET_KEY", "super-secret-jwt-key-change-in-production")
API_KEY = os.environ.get("API_KEY", "default_secret_api_key")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 30

def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()

def verify_password(plain_password: str, hashed_password: str) -> bool:
    return hash_password(plain_password) == hashed_password

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="token", auto_error=False)

# Fallback in-memory users (used only if D1 is unavailable or for initial seed)
fake_users_db = {
    "admin": {"username": "admin", "full_name": "Portal Admin", "hashed_password": hash_password("secret123"), "is_admin": True},
    "johndoe": {"username": "johndoe", "full_name": "John Doe", "hashed_password": hash_password("userpassword123"), "is_admin": False},
}
github_creds = {"info": {"user": "DefNotflexsman/server-att2"}}

# ═══════════════════════════════════════════════════════════════════════════════
# D1 DATABASE HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

async def get_db(request: Request):
    """Extract the D1 binding from the ASGI env (Cloudflare Workers)."""
    try:
        scope = request.scope
        env = scope.get("env") or scope.get("cloudflare", {}).get("env")
        return env.DB if env else None
    except Exception:
        return None

async def db_get_user_by_username(db, username: str) -> Optional[dict]:
    """Look up a user by username from D1. Returns dict or None."""
    if db is None:
        user = fake_users_db.get(username)
        if user:
            return {"username": user["username"], "full_name": user.get("full_name", ""),
                    "password_hash": user["hashed_password"], "role": "admin" if user.get("is_admin") else "user"}
        return None
    result = await db.prepare("SELECT id, username, email, password_hash, role FROM users WHERE username = ?").bind(username).all()
    rows = result.get("results", [])
    if not rows:
        return None
    row = rows[0]
    return {"id": row["id"], "username": row["username"], "email": row["email"],
            "password_hash": row["password_hash"], "role": row["role"]}

async def db_create_user(db, username: str, email: str, password: str, role: str = "user") -> dict:
    """Insert a new user into D1. Returns the created user dict."""
    password_hash = hash_password(password)
    salt = uuid_lib.uuid4().hex
    if db is None:
        # Fallback to in-memory
        fake_users_db[username] = {"username": username, "full_name": username,
                                   "hashed_password": password_hash, "is_admin": role == "admin"}
        return {"username": username, "email": email, "role": role}
    await db.prepare(
        "INSERT INTO users (username, email, password_hash, salt, role) VALUES (?, ?, ?, ?, ?)"
    ).bind(username, email, password_hash, salt, role).run()
    return {"username": username, "email": email, "role": role}

async def db_seed_if_empty(db):
    """Seed the D1 database with the default admin and user if empty."""
    if db is None:
        return
    try:
        result = await db.prepare("SELECT COUNT(*) as count FROM users").all()
        count = result.get("results", [{}])[0].get("count", 0)
        if count == 0:
            await db_create_user(db, "admin", "admin@portal.local", "secret123", role="admin")
            await db_create_user(db, "johndoe", "john@portal.local", "userpassword123", role="user")
            print("[DB] Seeded initial users into D1")
    except Exception as e:
        print(f"[DB] Seed failed (table may not exist yet): {e}")

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

# ═══════════════════════════════════════════════════════════════════════════════
# SHARED CSS — Trippy glassmorphism + neon + animations
# ═══════════════════════════════════════════════════════════════════════════════
SHARED_CSS = """
@import url('https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@300;400;500;600;700&family=JetBrains+Mono:wght@400;500;700&display=swap');
:root {
  --bg: #050510;
  --bg2: #0a0a1a;
  --glass: rgba(20, 20, 40, 0.6);
  --glass-border: rgba(255, 255, 255, 0.08);
  --neon-cyan: #00f0ff;
  --neon-purple: #b347ff;
  --neon-pink: #ff2d92;
  --neon-green: #00ff9d;
  --neon-orange: #ff6b1a;
  --text: #e8e8f0;
  --text-muted: #6a6a8a;
  --radius: 16px;
}
* { margin: 0; padding: 0; box-sizing: border-box; }
body {
  font-family: 'Space Grotesk', sans-serif;
  background: var(--bg);
  color: var(--text);
  min-height: 100vh;
  overflow-x: hidden;
}
/* Animated gradient background */
body::before {
  content: '';
  position: fixed;
  top: 0; left: 0; width: 100%; height: 100%;
  background:
    radial-gradient(ellipse at 20% 30%, rgba(179, 71, 255, 0.15), transparent 50%),
    radial-gradient(ellipse at 80% 70%, rgba(0, 240, 255, 0.12), transparent 50%),
    radial-gradient(ellipse at 50% 50%, rgba(255, 45, 146, 0.08), transparent 60%);
  z-index: -2;
  animation: bgshift 15s ease-in-out infinite alternate;
}
body::after {
  content: '';
  position: fixed;
  top: 0; left: 0; width: 100%; height: 100%;
  background-image:
    linear-gradient(rgba(255,255,255,0.015) 1px, transparent 1px),
    linear-gradient(90deg, rgba(255,255,255,0.015) 1px, transparent 1px);
  background-size: 50px 50px;
  z-index: -1;
  pointer-events: none;
}
@keyframes bgshift {
  0% { filter: hue-rotate(0deg); transform: scale(1); }
  100% { filter: hue-rotate(30deg); transform: scale(1.1); }
}
.glass {
  background: var(--glass);
  backdrop-filter: blur(20px);
  -webkit-backdrop-filter: blur(20px);
  border: 1px solid var(--glass-border);
  border-radius: var(--radius);
  box-shadow: 0 8px 32px rgba(0,0,0,0.4), inset 0 1px 0 rgba(255,255,255,0.05);
}
.container { max-width: 960px; margin: 0 auto; padding: 2rem 1.5rem; }
h1 { font-size: 2.5rem; font-weight: 700; letter-spacing: -0.03em; }
.gradient-text {
  background: linear-gradient(135deg, var(--neon-cyan), var(--neon-purple), var(--neon-pink));
  -webkit-background-clip: text;
  -webkit-text-fill-color: transparent;
  background-clip: text;
  background-size: 200% auto;
  animation: shimmer 3s linear infinite;
}
@keyframes shimmer { to { background-position: 200% center; } }
.subtitle { color: var(--text-muted); font-size: 1rem; margin-top: 0.25rem; }
.nav {
  display: flex; gap: 0.75rem; margin: 1.5rem 0; flex-wrap: wrap;
}
.nav a {
  padding: 0.5rem 1rem;
  border-radius: 10px;
  text-decoration: none;
  color: var(--text-muted);
  font-size: 0.85rem;
  font-weight: 500;
  transition: all 0.3s;
  border: 1px solid transparent;
}
.nav a:hover {
  color: var(--neon-cyan);
  border-color: rgba(0, 240, 255, 0.3);
  background: rgba(0, 240, 255, 0.05);
  box-shadow: 0 0 20px rgba(0, 240, 255, 0.15);
}
.card { padding: 1.5rem; margin-bottom: 1rem; }
.btn {
  display: inline-block;
  padding: 0.75rem 1.5rem;
  border-radius: 10px;
  border: none;
  font-family: inherit;
  font-size: 0.95rem;
  font-weight: 600;
  cursor: pointer;
  text-decoration: none;
  transition: all 0.3s;
  color: #050510;
  background: linear-gradient(135deg, var(--neon-cyan), var(--neon-purple));
  box-shadow: 0 4px 20px rgba(0, 240, 255, 0.3);
}
.btn:hover { transform: translateY(-2px); box-shadow: 0 8px 30px rgba(0, 240, 255, 0.5); }
.btn:active { transform: translateY(0); }
.btn-secondary {
  background: transparent;
  color: var(--text);
  border: 1px solid var(--glass-border);
  box-shadow: none;
}
.btn-secondary:hover { border-color: var(--neon-purple); color: var(--neon-purple); box-shadow: 0 0 20px rgba(179, 71, 255, 0.2); }
input, textarea, select {
  width: 100%;
  background: rgba(5, 5, 16, 0.6);
  border: 1px solid var(--glass-border);
  border-radius: 10px;
  padding: 0.75rem 1rem;
  color: var(--text);
  font-family: inherit;
  font-size: 0.95rem;
  outline: none;
  transition: all 0.3s;
}
input:focus, textarea:focus, select:focus {
  border-color: var(--neon-cyan);
  box-shadow: 0 0 0 3px rgba(0, 240, 255, 0.1);
}
label { display: block; font-size: 0.75rem; text-transform: uppercase; letter-spacing: 0.1em; color: var(--text-muted); margin-bottom: 0.5rem; }
.field { margin-bottom: 1.25rem; }
pre, .code-block {
  background: rgba(5, 5, 16, 0.8);
  border: 1px solid var(--glass-border);
  border-radius: 10px;
  padding: 1rem;
  font-family: 'JetBrains Mono', monospace;
  font-size: 0.85rem;
  overflow-x: auto;
  white-space: pre-wrap;
  word-break: break-all;
  color: var(--neon-green);
}
.badge {
  display: inline-block;
  padding: 0.2rem 0.6rem;
  border-radius: 6px;
  font-size: 0.7rem;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  font-family: 'JetBrains Mono', monospace;
}
.badge-get { background: rgba(0, 240, 255, 0.15); color: var(--neon-cyan); border: 1px solid rgba(0, 240, 255, 0.3); }
.badge-post { background: rgba(0, 255, 157, 0.15); color: var(--neon-green); border: 1px solid rgba(0, 255, 157, 0.3); }
.badge-ws { background: rgba(255, 107, 26, 0.15); color: var(--neon-orange); border: 1px solid rgba(255, 107, 26, 0.3); }
.badge-admin { background: rgba(255, 45, 146, 0.15); color: var(--neon-pink); border: 1px solid rgba(255, 45, 146, 0.3); }
.route-row {
  display: flex; align-items: center; gap: 1rem;
  padding: 1rem 1.25rem;
  margin-bottom: 0.5rem;
  border-radius: 12px;
  background: rgba(20, 20, 40, 0.3);
  border: 1px solid transparent;
  transition: all 0.3s;
  cursor: pointer;
  text-decoration: none;
  color: inherit;
}
.route-row:hover {
  border-color: var(--glass-border);
  background: rgba(20, 20, 40, 0.5);
  transform: translateX(4px);
}
.route-row .endpoint { font-family: 'JetBrains Mono', monospace; font-size: 0.95rem; flex-grow: 1; }
.route-row .desc { color: var(--text-muted); font-size: 0.85rem; }
.section-label {
  font-size: 0.7rem; text-transform: uppercase; letter-spacing: 0.15em;
  color: var(--text-muted); margin: 1.5rem 0 0.75rem; padding-left: 0.25rem;
}
.stat-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 1rem; }
.stat-card { padding: 1.25rem; text-align: center; }
.stat-card .stat-value { font-size: 2rem; font-weight: 700; font-family: 'JetBrains Mono', monospace; }
.stat-card .stat-label { font-size: 0.75rem; text-transform: uppercase; letter-spacing: 0.1em; color: var(--text-muted); margin-top: 0.25rem; }
.glow-cyan { color: var(--neon-cyan); text-shadow: 0 0 20px rgba(0,240,255,0.5); }
.glow-purple { color: var(--neon-purple); text-shadow: 0 0 20px rgba(179,71,255,0.5); }
.glow-pink { color: var(--neon-pink); text-shadow: 0 0 20px rgba(255,45,146,0.5); }
.glow-green { color: var(--neon-green); text-shadow: 0 0 20px rgba(0,255,157,0.5); }
.glow-orange { color: var(--neon-orange); text-shadow: 0 0 20px rgba(255,107,26,0.5); }
.output-box {
  margin-top: 1.5rem;
  min-height: 80px;
  max-height: 400px;
  overflow-y: auto;
}
.pulse-dot {
  display: inline-block; width: 8px; height: 8px; border-radius: 50%;
  background: var(--neon-green); box-shadow: 0 0 10px var(--neon-green);
  animation: pulse 1.5s ease-in-out infinite;
}
@keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.3; } }
.ws-message { padding: 0.5rem 0.75rem; margin-bottom: 0.5rem; border-radius: 8px; font-family: 'JetBrains Mono', monospace; font-size: 0.85rem; }
.ws-message.sent { background: rgba(0, 240, 255, 0.1); border-left: 3px solid var(--neon-cyan); }
.ws-message.received { background: rgba(0, 255, 157, 0.1); border-left: 3px solid var(--neon-green); }
.ws-message.system { background: rgba(255, 45, 146, 0.1); border-left: 3px solid var(--neon-pink); }
.loading-spinner {
  display: inline-block; width: 18px; height: 18px;
  border: 2px solid var(--glass-border); border-top-color: var(--neon-cyan);
  border-radius: 50%; animation: spin 0.6s linear infinite;
}
@keyframes spin { to { transform: rotate(360deg); } }
.footer { text-align: center; padding: 2rem 0; color: var(--text-muted); font-size: 0.8rem; }
.footer a { color: var(--neon-purple); text-decoration: none; }
.chart-bar {
  height: 8px; border-radius: 4px;
  background: linear-gradient(90deg, var(--neon-cyan), var(--neon-purple));
  transition: width 1s ease;
}
.activity-log {
  max-height: 200px; overflow-y: auto;
}
.activity-log .log-entry {
  padding: 0.5rem 0.75rem; margin-bottom: 0.25rem; border-radius: 6px;
  background: rgba(5, 5, 16, 0.4); font-family: 'JetBrains Mono', monospace;
  font-size: 0.8rem; color: var(--text-muted);
}
.activity-log .log-entry .timestamp { color: var(--neon-cyan); }
.toast {
  position: fixed; top: 20px; right: 20px;
  padding: 1rem 1.5rem; border-radius: 10px;
  background: var(--glass); backdrop-filter: blur(20px);
  border: 1px solid var(--glass-border);
  box-shadow: 0 8px 32px rgba(0,0,0,0.4);
  z-index: 999; transform: translateX(400px);
  transition: transform 0.3s; font-size: 0.9rem;
}
.toast.show { transform: translateX(0); }
.toast.success { border-color: var(--neon-green); }
.toast.error { border-color: var(--neon-pink); }
@media (max-width: 640px) {
  .container { padding: 1rem; }
  h1 { font-size: 1.8rem; }
  .nav { flex-direction: column; }
  .stat-grid { grid-template-columns: 1fr 1fr; }
}
"""

# ═══════════════════════════════════════════════════════════════════════════════
# LANDING PAGE — Full interactive API directory
# ═══════════════════════════════════════════════════════════════════════════════
LANDING_PAGE = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Asynchronous Portal Engine</title><style>{SHARED_CSS}</style></head>
<body><div class="container">
  <div class="glass card" style="text-align:center;padding:3rem 2rem;">
    <h1 class="gradient-text">Asynchronous Portal Engine</h1>
    <p class="subtitle">Powered by Cloudflare Workers &mdash; FastAPI on the Edge</p>
    <div style="margin-top:1rem;"><span class="pulse-dot"></span> <span style="color:var(--neon-green);font-size:0.85rem;">All systems operational</span></div>
  </div>

  <nav class="nav" style="justify-content:center;">
    <a href="/admindashboard">Admin Dashboard</a>
    <a href="/ws-client">WebSocket Client</a>
    <a href="/api-explorer">API Explorer</a>
    <a href="/token-generator">Token Generator</a>
    <a href="/uuid-generator">UUID Generator</a>
    <a href="/item-lookup">Item Lookup</a>
    <a href="/github-info">GitHub Info</a>
    <a href="/docs">Swagger Docs</a>
  </nav>

  <div class="glass card">
    <div class="section-label">General &amp; UI</div>
    <a class="route-row" href="/"><span class="badge badge-get">GET</span><span class="endpoint">/</span><span class="desc">API Directory (this page)</span></a>
    <a class="route-row" href="/admindashboard"><span class="badge badge-get">GET</span><span class="endpoint">/admindashboard</span><span class="desc">Admin Control Panel</span></a>
    <a class="route-row" href="/docs"><span class="badge badge-get">GET</span><span class="endpoint">/docs</span><span class="desc">Swagger UI</span></a>

    <div class="section-label">Authentication</div>
    <a class="route-row" href="/token-generator"><span class="badge badge-post">POST</span><span class="endpoint">/token</span><span class="desc">Get JWT Bearer Token</span></a>
    <a class="route-row" href="/register"><span class="badge badge-post">POST</span><span class="endpoint">/register</span><span class="desc">Create a new account</span></a>
    <a class="route-row" href="/api-explorer"><span class="badge badge-get">GET</span><span class="endpoint">/api/authentication</span><span class="desc">Validate X-API-Key</span></a>

    <div class="section-label">Core APIs</div>
    <a class="route-row" href="/api-explorer"><span class="badge badge-post">POST</span><span class="endpoint">/api/request</span><span class="desc">Inspect Request Metadata</span></a>
    <a class="route-row" href="/item-lookup"><span class="badge badge-get">GET</span><span class="endpoint">/api/items/{{item_id}}</span><span class="desc">Retrieve Item Details</span></a>
    <a class="route-row" href="/uuid-generator"><span class="badge badge-get">GET</span><span class="endpoint">/api/status</span><span class="desc">Generate UUIDs</span></a>

    <div class="section-label">Admin Routes (Protected)</div>
    <a class="route-row" href="/admindashboard"><span class="badge badge-admin">ADMIN</span><span class="endpoint">/api/admin/metrics</span><span class="desc">Server Statistics</span></a>
    <a class="route-row" href="/admindashboard"><span class="badge badge-admin">ADMIN</span><span class="endpoint">/api/server/mc</span><span class="desc">Launch Background Process</span></a>
    <a class="route-row" href="/github-info"><span class="badge badge-get">GET</span><span class="endpoint">/api/github/info</span><span class="desc">GitHub Repository Info</span></a>

    <div class="section-label">WebSockets</div>
    <a class="route-row" href="/ws-client"><span class="badge badge-ws">WS</span><span class="endpoint">/ws</span><span class="desc">WebSocket Gateway</span></a>
    <a class="route-row" href="/ws-client"><span class="badge badge-ws">WS</span><span class="endpoint">/server/accept</span><span class="desc">Secondary WebSocket Endpoint</span></a>
  </div>

  <div class="footer">Running on <a href="https://workers.cloudflare.com">Cloudflare Workers</a> &middot; FastAPI + Python</div>
</div></body></html>"""

# ═══════════════════════════════════════════════════════════════════════════════
# ADMIN LOGIN PAGE
# ═══════════════════════════════════════════════════════════════════════════════
ADMIN_LOGIN = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Admin Login &mdash; Portal Engine</title><style>{SHARED_CSS}</style></head>
<body><div class="container" style="max-width:420px;display:flex;align-items:center;min-height:100vh;">
  <div class="glass card" style="width:100%;padding:2.5rem;">
    <div style="text-align:center;margin-bottom:1.5rem;">
      <div style="font-size:3rem;">🔐</div>
      <h1 class="gradient-text" style="font-size:1.8rem;">Admin Access</h1>
      <p class="subtitle">Authenticate to access the control panel</p>
    </div>
    <form action="/admindashboard/login" method="POST">
      <div class="field"><label>Username</label><input type="text" name="username" required placeholder="admin"></div>
      <div class="field"><label>Password</label><input type="password" name="password" required placeholder="••••••••"></div>
      <button type="submit" class="btn" style="width:100%;">Authenticate</button>
    </form>
    <div style="text-align:center;margin-top:1rem;">
      <a href="/" style="color:var(--text-muted);font-size:0.85rem;text-decoration:none;">&larr; Back to directory</a>
    </div>
  </div>
</div></body></html>"""

# ═══════════════════════════════════════════════════════════════════════════════
# REGISTRATION PAGE
# ═══════════════════════════════════════════════════════════════════════════════
REGISTER_PAGE = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Register &mdash; Portal Engine</title><style>{SHARED_CSS}</style></head>
<body><div class="container" style="max-width:420px;display:flex;align-items:center;min-height:100vh;">
  <div class="glass card" style="width:100%;padding:2.5rem;">
    <div style="text-align:center;margin-bottom:1.5rem;">
      <div style="font-size:3rem;">📝</div>
      <h1 class="gradient-text" style="font-size:1.8rem;">Create Account</h1>
      <p class="subtitle">Register a new user (shared across all portal sites)</p>
    </div>
    <form action="/register" method="POST">
      <div class="field"><label>Username</label><input type="text" name="username" required placeholder="newuser" minlength="3"></div>
      <div class="field"><label>Email</label><input type="email" name="email" required placeholder="user@example.com"></div>
      <div class="field"><label>Password</label><input type="password" name="password" required placeholder="••••••••" minlength="6"></div>
      <button type="submit" class="btn" style="width:100%;">Register</button>
    </form>
    <div style="text-align:center;margin-top:1rem;">
      <a href="/token-generator" style="color:var(--text-muted);font-size:0.85rem;text-decoration:none;">&larr; Already have an account? Get a token</a>
    </div>
    <div style="text-align:center;margin-top:0.5rem;">
      <a href="/" style="color:var(--text-muted);font-size:0.85rem;text-decoration:none;">&larr; Back to directory</a>
    </div>
  </div>
</div></body></html>"""

# ═══════════════════════════════════════════════════════════════════════════════
# ADMIN DASHBOARD — Advanced with stats, charts, controls, activity log
# ═══════════════════════════════════════════════════════════════════════════════
ADMIN_DASHBOARD = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Admin Dashboard &mdash; Portal Engine</title><style>{SHARED_CSS}</style></head>
<body><div class="container">
  <div class="glass card" style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:1rem;">
    <div>
      <h1 class="gradient-text">Control Panel</h1>
      <p class="subtitle"><span class="pulse-dot"></span> Authenticated as <span class="glow-cyan">admin</span></p>
    </div>
    <div style="display:flex;gap:0.5rem;">
      <a href="/" class="btn btn-secondary">Home</a>
      <a href="/admindashboard/logout" class="btn btn-secondary" style="color:var(--neon-pink);">Logout</a>
    </div>
  </div>

  <!-- Stats Grid -->
  <div class="stat-grid" style="margin-top:1rem;">
    <div class="glass stat-card">
      <div class="stat-value glow-cyan" id="stat-uptime">99.9%</div>
      <div class="stat-label">Uptime</div>
    </div>
    <div class="glass stat-card">
      <div class="stat-value glow-purple" id="stat-nodes">4</div>
      <div class="stat-label">Active Nodes</div>
    </div>
    <div class="glass stat-card">
      <div class="stat-value glow-green" id="stat-requests">1,024</div>
      <div class="stat-label">Requests Processed</div>
    </div>
    <div class="glass stat-card">
      <div class="stat-value glow-orange" id="stat-latency">12ms</div>
      <div class="stat-label">Avg Latency</div>
    </div>
  </div>

  <!-- Request Chart -->
  <div class="glass card" style="margin-top:1rem;">
    <div class="section-label">Request Volume (last 12 hours)</div>
    <div id="chart" style="display:flex;align-items:flex-end;gap:4px;height:120px;padding-top:1rem;"></div>
    <div style="display:flex;justify-content:space-between;font-size:0.7rem;color:var(--text-muted);margin-top:0.5rem;font-family:'JetBrains Mono',monospace;">
      <span>12h ago</span><span>6h ago</span><span>now</span>
    </div>
  </div>

  <!-- Server Controls -->
  <div class="glass card" style="margin-top:1rem;">
    <div class="section-label">Server Controls</div>
    <div style="display:flex;gap:0.75rem;flex-wrap:wrap;margin-top:0.5rem;">
      <button class="btn" onclick="launchMC()" id="mc-btn">Launch MC Server</button>
      <button class="btn btn-secondary" onclick="fetchMetrics()">Refresh Metrics</button>
      <button class="btn btn-secondary" onclick="inspectRequest()">Inspect Request</button>
    </div>
    <div id="control-output" class="output-box"></div>
  </div>

  <!-- Token & API Key -->
  <div class="glass card" style="margin-top:1rem;">
    <div class="section-label">Authentication</div>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:1rem;margin-top:0.5rem;">
      <div>
        <label>JWT Token</label>
        <input type="text" id="jwt-token" readonly placeholder="Generate a token first">
        <button class="btn btn-secondary" style="margin-top:0.5rem;width:100%;" onclick="getToken()">Get Token</button>
      </div>
      <div>
        <label>API Key</label>
        <input type="text" value="{API_KEY}" readonly>
        <button class="btn btn-secondary" style="margin-top:0.5rem;width:100%;" onclick="testApiKey()">Test API Key</button>
      </div>
    </div>
  </div>

  <!-- Activity Log -->
  <div class="glass card" style="margin-top:1rem;">
    <div class="section-label">Activity Log</div>
    <div class="activity-log" id="activity-log">
      <div class="log-entry"><span class="timestamp">[{datetime.utcnow().strftime('%H:%M:%S')}]</span> Dashboard loaded</div>
    </div>
  </div>

  <div class="footer">Portal Engine v1.0 &middot; <a href="https://workers.cloudflare.com">Cloudflare Workers</a></div>
</div>

<script>
function logActivity(msg) {{
  const log = document.getElementById('activity-log');
  const entry = document.createElement('div');
  entry.className = 'log-entry';
  const now = new Date().toLocaleTimeString();
  entry.innerHTML = '<span class="timestamp">[' + now + ']</span> ' + msg;
  log.insertBefore(entry, log.firstChild);
}}

// Generate chart bars
(function() {{
  const chart = document.getElementById('chart');
  for (let i = 0; i < 24; i++) {{
    const bar = document.createElement('div');
    bar.style.cssText = 'flex:1;border-radius:4px 4px 0 0;background:linear-gradient(180deg,var(--neon-cyan),var(--neon-purple));opacity:0.7;transition:opacity 0.3s;';
    bar.style.height = (20 + Math.random() * 80) + '%';
    bar.title = 'Hour ' + (i+1);
    bar.onmouseenter = () => bar.style.opacity = '1';
    bar.onmouseleave = () => bar.style.opacity = '0.7';
    chart.appendChild(bar);
  }}
}})();

async function getToken() {{
  logActivity('Requesting JWT token...');
  const res = await fetch('/token', {{
    method: 'POST',
    headers: {{'Content-Type': 'application/x-www-form-urlencoded'}},
    body: 'username=admin&password=secret123'
  }});
  if (res.ok) {{
    const data = await res.json();
    document.getElementById('jwt-token').value = data.access_token;
    logActivity('✅ JWT token obtained');
    showToast('Token generated!', 'success');
  }} else {{
    logActivity('❌ Failed to get token');
    showToast('Failed to get token', 'error');
  }}
}}

async function testApiKey() {{
  logActivity('Testing API key...');
  const res = await fetch('/api/authentication', {{headers: {{'X-API-Key': '{API_KEY}'}}}});
async function fetchMetrics() {{
  logActivity('Fetching admin metrics...');
  let token = document.getElementById('jwt-token').value;
  if (!token) {{ await getToken(); token = document.getElementById('jwt-token').value; }}
  const res = await fetch('/api/admin/metrics', {{headers: {{'Authorization': 'Bearer ' + token}}}});
  const text = await res.text();
  document.getElementById('control-output').innerHTML = '<pre>' + text + '</pre>';
  if (res.ok) {{
    try {{
      const data = JSON.parse(text);
      document.getElementById('stat-requests').textContent = data.metrics.requests_processed.toLocaleString();
      document.getElementById('stat-nodes').textContent = data.metrics.active_nodes;
      document.getElementById('stat-uptime').textContent = data.metrics.uptime;
    }} catch(e) {{}}
  }}
  logActivity(res.ok ? '✅ Metrics retrieved' : '❌ Metrics failed');
}}

async function inspectRequest() {{
  logActivity('Inspecting request metadata...');
  const res = await fetch('/api/request', {{method: 'POST'}});
  const text = await res.text();
  document.getElementById('control-output').innerHTML = '<pre>' + text + '</pre>';
  logActivity('✅ Request inspected');
}}

function showToast(msg, type) {{
  const toast = document.createElement('div');
  toast.className = 'toast ' + type;
  toast.textContent = msg;
  document.body.appendChild(toast);
  setTimeout(() => toast.classList.add('show'), 10);
  setTimeout(() => {{ toast.remove(); }}, 3000);
}}
</script>
</body></html>"""

# ═══════════════════════════════════════════════════════════════════════════════
# WEBSOCKET CLIENT PAGE
# ═══════════════════════════════════════════════════════════════════════════════
WS_CLIENT_PAGE = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>WebSocket Client &mdash; Portal Engine</title><style>{SHARED_CSS}</style></head>
<body><div class="container">
  <div class="glass card" style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:1rem;">
    <div><h1 class="gradient-text">WebSocket Client</h1><p class="subtitle">Real-time gateway connection</p></div>
    <a href="/" class="btn btn-secondary">Home</a>
  </div>

  <div class="glass card" style="margin-top:1rem;">
    <div class="field">
      <label>Endpoint</label>
      <select id="ws-endpoint">
        <option value="/ws">/ws &mdash; WebSocket Gateway</option>
        <option value="/server/accept">/server/accept &mdash; Secondary Endpoint</option>
      </select>
    </div>
    <div style="display:flex;gap:0.5rem;">
      <button class="btn" onclick="connectWS()" id="connect-btn">Connect</button>
      <button class="btn btn-secondary" onclick="disconnectWS()" id="disconnect-btn" disabled>Disconnect</button>
    </div>
    <div style="margin-top:0.75rem;font-size:0.85rem;">
      <span id="ws-status" style="color:var(--text-muted);">● Disconnected</span>
    </div>
  </div>

  <div class="glass card" style="margin-top:1rem;">
    <div class="section-label">Messages</div>
    <div id="ws-messages" style="min-height:200px;max-height:350px;overflow-y:auto;margin-top:0.5rem;"></div>
    <div style="display:flex;gap:0.5rem;margin-top:1rem;">
      <input type="text" id="ws-input" placeholder="Type a message..." onkeydown="if(event.key==='Enter')sendWS()">
      <button class="btn" onclick="sendWS()" id="send-btn" disabled>Send</button>
    </div>
  </div>

  <div class="footer">WebSocket Client &middot; <a href="https://workers.cloudflare.com">Cloudflare Workers</a></div>
</div>

<script>
let ws = null;
function addMsg(text, type) {{
  const div = document.getElementById('ws-messages');
  const msg = document.createElement('div');
  msg.className = 'ws-message ' + type;
  const now = new Date().toLocaleTimeString();
  msg.innerHTML = '<span style="color:var(--text-muted);font-size:0.75rem;">[' + now + ']</span> ' + text;
  div.appendChild(msg);
  div.scrollTop = div.scrollHeight;
}}
function connectWS() {{
  const endpoint = document.getElementById('ws-endpoint').value;
  const url = (location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + endpoint;
  addMsg('Connecting to ' + url + '...', 'system');
  ws = new WebSocket(url);
  ws.onopen = () => {{
    document.getElementById('ws-status').innerHTML = '<span style="color:var(--neon-green);">● Connected</span>';
    document.getElementById('connect-btn').disabled = true;
    document.getElementById('disconnect-btn').disabled = false;
    document.getElementById('send-btn').disabled = false;
    addMsg('✅ Connection established', 'system');
  }};
  ws.onmessage = (e) => addMsg(e.data, 'received');
  ws.onclose = () => {{
    document.getElementById('ws-status').innerHTML = '<span style="color:var(--text-muted);">● Disconnected</span>';
    document.getElementById('connect-btn').disabled = false;
    document.getElementById('disconnect-btn').disabled = true;
    document.getElementById('send-btn').disabled = true;
    addMsg('Connection closed', 'system');
  }};
  ws.onerror = () => addMsg('❌ Connection error', 'system');
}}
function disconnectWS() {{ if (ws) ws.close(); }}
function sendWS() {{
  const input = document.getElementById('ws-input');
  if (ws && ws.readyState === 1 && input.value.trim()) {{
    addMsg(input.value, 'sent');
    ws.send(input.value);
    input.value = '';
  }}
}}
</script>
</body></html>"""

# ═══════════════════════════════════════════════════════════════════════════════
# API EXPLORER PAGE
# ═══════════════════════════════════════════════════════════════════════════════
API_EXPLORER_PAGE = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>API Explorer &mdash; Portal Engine</title><style>{SHARED_CSS}</style></head>
<body><div class="container">
  <div class="glass card" style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:1rem;">
    <div><h1 class="gradient-text">API Explorer</h1><p class="subtitle">Test API endpoints interactively</p></div>
    <a href="/" class="btn btn-secondary">Home</a>
  </div>

  <div class="glass card" style="margin-top:1rem;">
    <div class="field">
      <label>Endpoint</label>
      <select id="api-endpoint">
        <option value="/api/authentication|GET">GET /api/authentication (API Key)</option>
        <option value="/api/request|POST">POST /api/request (Inspect)</option>
        <option value="/api/endpoint/test|GET">GET /api/endpoint/test</option>
        <option value="/api/endpoint/test|POST">POST /api/endpoint/test</option>
      </select>
    </div>
    <div class="field" id="api-key-field">
      <label>X-API-Key Header</label>
      <input type="text" id="api-key-input" value="{API_KEY}">
    </div>
    <button class="btn" onclick="sendApiRequest()" id="api-btn">Send Request</button>
    <div id="api-output" class="output-box"></div>
  </div>

  <div class="footer">API Explorer &middot; <a href="https://workers.cloudflare.com">Cloudflare Workers</a></div>
</div>

<script>
async function sendApiRequest() {{
  const btn = document.getElementById('api-btn');
  btn.disabled = true; btn.innerHTML = '<span class="loading-spinner"></span> Sending...';
  const [path, method] = document.getElementById('api-endpoint').value.split('|');
  const apiKey = document.getElementById('api-key-input').value;
  const headers = {{}};
  if (path === '/api/authentication') headers['X-API-Key'] = apiKey;
  const res = await fetch(path, {{method, headers}});
  const text = await res.text();
  let display = text;
  try {{ display = JSON.stringify(JSON.parse(text), null, 2); }} catch(e) {{}}
  document.getElementById('api-output').innerHTML = '<pre>' + display + '</pre>';
  btn.disabled = false; btn.textContent = 'Send Request';
}}
document.getElementById('api-endpoint').addEventListener('change', () => {{
  const val = document.getElementById('api-endpoint').value;
  document.getElementById('api-key-field').style.display = val.startsWith('/api/authentication') ? 'block' : 'none';
}});
</script>
</body></html>"""

# ═══════════════════════════════════════════════════════════════════════════════
# TOKEN GENERATOR PAGE
# ═══════════════════════════════════════════════════════════════════════════════
TOKEN_PAGE = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Token Generator &mdash; Portal Engine</title><style>{SHARED_CSS}</style></head>
<body><div class="container" style="max-width:600px;">
  <div class="glass card" style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:1rem;">
    <div><h1 class="gradient-text">Token Generator</h1><p class="subtitle">Obtain a JWT bearer token</p></div>
    <a href="/" class="btn btn-secondary">Home</a>
  </div>

  <div class="glass card" style="margin-top:1rem;">
    <div class="field"><label>Username</label><input type="text" id="tok-user" value="admin"></div>
    <div class="field"><label>Password</label><input type="password" id="tok-pass" value="secret123"></div>
    <button class="btn" onclick="genToken()" id="tok-btn">Generate Token</button>
    <div id="tok-output" class="output-box"></div>
  </div>

  <div class="footer">Token Generator &middot; <a href="https://workers.cloudflare.com">Cloudflare Workers</a></div>
</div>

<script>
async function genToken() {{
  const btn = document.getElementById('tok-btn');
  btn.disabled = true; btn.innerHTML = '<span class="loading-spinner"></span> Generating...';
  const user = document.getElementById('tok-user').value;
  const pass = document.getElementById('tok-pass').value;
  const res = await fetch('/token', {{
    method: 'POST',
    headers: {{'Content-Type': 'application/x-www-form-urlencoded'}},
    body: 'username=' + encodeURIComponent(user) + '&password=' + encodeURIComponent(pass)
  }});
  const text = await res.text();
  let display = text;
  try {{ display = JSON.stringify(JSON.parse(text), null, 2); }} catch(e) {{}}
  document.getElementById('tok-output').innerHTML = '<pre>' + display + '</pre>';
  btn.disabled = false; btn.textContent = 'Generate Token';
}}
</script>
</body></html>"""

# ═══════════════════════════════════════════════════════════════════════════════
# UUID GENERATOR PAGE
# ═══════════════════════════════════════════════════════════════════════════════
UUID_PAGE = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>UUID Generator &mdash; Portal Engine</title><style>{SHARED_CSS}</style></head>
<body><div class="container" style="max-width:600px;">
  <div class="glass card" style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:1rem;">
    <div><h1 class="gradient-text">UUID Generator</h1><p class="subtitle">Generate UUIDs via external API</p></div>
    <a href="/" class="btn btn-secondary">Home</a>
  </div>

  <div class="glass card" style="margin-top:1rem;">
    <div class="field"><label>Amount (1-1000)</label><input type="number" id="uuid-amount" value="10" min="1" max="1000"></div>
    <button class="btn" onclick="genUUIDs()" id="uuid-btn">Generate</button>
    <div id="uuid-output" class="output-box"></div>
  </div>

  <div class="footer">UUID Generator &middot; <a href="https://workers.cloudflare.com">Cloudflare Workers</a></div>
</div>

<script>
async function genUUIDs() {{
  const btn = document.getElementById('uuid-btn');
  btn.disabled = true; btn.innerHTML = '<span class="loading-spinner"></span> Generating...';
  const amount = document.getElementById('uuid-amount').value;
  const res = await fetch('/api/status?amount=' + amount);
  const text = await res.text();
  let display = text;
  try {{
    const data = JSON.parse(text);
    display = data.uuids.map((u, i) => (i+1) + '. ' + u).join('\\n');
  }} catch(e) {{}}
  document.getElementById('uuid-output').innerHTML = '<pre>' + display + '</pre>';
  btn.disabled = false; btn.textContent = 'Generate';
}}
</script>
</body></html>"""

# ═══════════════════════════════════════════════════════════════════════════════
# ITEM LOOKUP PAGE
# ═══════════════════════════════════════════════════════════════════════════════
ITEM_PAGE = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Item Lookup &mdash; Portal Engine</title><style>{SHARED_CSS}</style></head>
<body><div class="container" style="max-width:600px;">
  <div class="glass card" style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:1rem;">
    <div><h1 class="gradient-text">Item Lookup</h1><p class="subtitle">Retrieve item details by ID</p></div>
    <a href="/" class="btn btn-secondary">Home</a>
  </div>

  <div class="glass card" style="margin-top:1rem;">
    <div class="field"><label>Item ID (1-100)</label><input type="number" id="item-id" value="5" min="1" max="100"></div>
    <button class="btn" onclick="lookupItem()" id="item-btn">Look Up</button>
    <div id="item-output" class="output-box"></div>
  </div>

  <div class="footer">Item Lookup &middot; <a href="https://workers.cloudflare.com">Cloudflare Workers</a></div>
</div>

<script>
async function lookupItem() {{
  const btn = document.getElementById('item-btn');
  btn.disabled = true; btn.innerHTML = '<span class="loading-spinner"></span> Looking up...';
  const id = document.getElementById('item-id').value;
  const res = await fetch('/api/items/' + id);
  const text = await res.text();
  let display = text;
  try {{ display = JSON.stringify(JSON.parse(text), null, 2); }} catch(e) {{}}
  document.getElementById('item-output').innerHTML = '<pre>' + display + '</pre>';
  btn.disabled = false; btn.textContent = 'Look Up';
}}
</script>
</body></html>"""

# ═══════════════════════════════════════════════════════════════════════════════
# GITHUB INFO PAGE
# ═══════════════════════════════════════════════════════════════════════════════
GITHUB_PAGE = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>GitHub Info &mdash; Portal Engine</title><style>{SHARED_CSS}</style></head>
<body><div class="container" style="max-width:700px;">
  <div class="glass card" style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:1rem;">
    <div><h1 class="gradient-text">GitHub Info</h1><p class="subtitle">Repository details from GitHub API</p></div>
    <a href="/" class="btn btn-secondary">Home</a>
  </div>

  <div class="glass card" style="margin-top:1rem;">
    <button class="btn" onclick="fetchGitHub()" id="gh-btn">Fetch Repository Info</button>
    <div id="gh-output" class="output-box"></div>
  </div>

  <div class="footer">GitHub Info &middot; <a href="https://workers.cloudflare.com">Cloudflare Workers</a></div>
</div>

<script>
async function fetchGitHub() {{
  const btn = document.getElementById('gh-btn');
  btn.disabled = true; btn.innerHTML = '<span class="loading-spinner"></span> Fetching...';
  const res = await fetch('/api/github/info');
  const text = await res.text();
  let display = text;
  try {{
    const data = JSON.parse(text);
    const r = data.data;
    display = 'Repository: ' + (r.full_name || 'N/A') + '\\n' +
      'Description: ' + (r.description || 'N/A') + '\\n' +
      'Stars: ' + (r.stargazers_count || 0) + '\\n' +
      'Forks: ' + (r.forks_count || 0) + '\\n' +
      'Open Issues: ' + (r.open_issues_count || 0) + '\\n' +
      'Language: ' + (r.language || 'N/A') + '\\n' +
      'Created: ' + (r.created_at || 'N/A') + '\\n' +
      'Updated: ' + (r.updated_at || 'N/A') + '\\n' +
      'URL: ' + (r.html_url || 'N/A');
  }} catch(e) {{}}
  document.getElementById('gh-output').innerHTML = '<pre>' + display + '</pre>';
  btn.disabled = false; btn.textContent = 'Fetch Repository Info';
}}
</script>
</body></html>"""

# ═══════════════════════════════════════════════════════════════════════════════
# FASTAPI APP
# ═══════════════════════════════════════════════════════════════════════════════
app = FastAPI(debug=True, title="Asynchronous Portal Engine", description="FastAPI on Cloudflare Workers")

def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    to_encode = data.copy()
    expire = datetime.utcnow() + (expires_delta or timedelta(minutes=15))
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)

async def get_current_user(request: Request, token: str = Depends(oauth2_scheme)) -> User:
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
    db = await get_db(request)
    user_dict = await db_get_user_by_username(db, username)
    if user_dict is None:
        raise credentials_exception
    return User(username=user_dict["username"], full_name=user_dict.get("email", ""), is_admin=user_dict.get("role") == "admin")

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

@app.middleware("http")
async def log_middleware(request: Request, call_next):
    start_time = time.time()
    if request.url.path.startswith("/api/"):
        print(f"[API] {request.method} | {request.url.path}")
    response = await call_next(request)
    response.headers["X-Process-Time"] = str(time.time() - start_time)
    return response

# ═══════════════════════════════════════════════════════════════════════════════
# API ENDPOINTS (curl-compatible)
# ═══════════════════════════════════════════════════════════════════════════════

@app.post("/api/request", tags=["API Parser"])
async def inspect_request_endpoint(request: Request):
    return {"status": "success", "parsed_request": {"path": request.url.path, "method": request.method, "client_ip": request.client.host if request.client else "Unknown", "headers": dict(request.headers), "query_params": dict(request.query_params), "timestamp": datetime.utcnow().isoformat()}}

@app.post("/token", response_model=Token, tags=["Auth"])
async def login_for_access_token(request: Request, form_data: OAuth2PasswordRequestForm = Depends()):
    db = await get_db(request)
    await db_seed_if_empty(db)
    user_dict = await db_get_user_by_username(db, form_data.username)
    if not user_dict or not verify_password(form_data.password, user_dict["password_hash"]):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Incorrect username or password", headers={"WWW-Authenticate": "Bearer"})
    # Update last_login timestamp
    if db is not None:
        try:
            await db.prepare("UPDATE users SET last_login = datetime('now') WHERE username = ?").bind(form_data.username).run()
        except Exception:
            pass
    access_token = create_access_token(data={"sub": user_dict["username"]}, expires_delta=timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES))
    return {"access_token": access_token, "token_type": "bearer"}

@app.get("/api/items/{item_id}", response_model=ItemResponse, tags=["Items"])
async def read_item(item_id: int = Path(..., ge=1)):
    if item_id > 100:
        raise ItemNotFoundException(item_id=item_id)
    return {"item_id": item_id, "name": f"Sample Item #{item_id}"}

@app.get("/api/status", response_model=UUIDResponse, tags=["External APIs"])
async def get_uuid_status(amount: int = Query(10, ge=1, le=1000)):
    import httpx
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
    import httpx
    repo = github_creds["info"]["user"]
    async with httpx.AsyncClient() as client:
        try:
            response = await client.get(f"https://api.github.com/repos/{repo}", headers={"User-Agent": "FastAPI-Portal-Engine"}, timeout=10.0)
            if response.status_code != 200:
                raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"GitHub API error (HTTP {response.status_code}).")
            return {"status": "success", "repository": repo, "data": response.json()}
        except httpx.RequestError as exc:
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=f"Network error: {exc}")

# ═══════════════════════════════════════════════════════════════════════════════
# HTML PAGES
# ═══════════════════════════════════════════════════════════════════════════════

@app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def home():
    return HTMLResponse(content=LANDING_PAGE)

@app.get("/admindashboard", response_class=HTMLResponse, include_in_schema=False)
async def admin_dashboard_page(admin_session: Optional[str] = Cookie(None)):
    if admin_session == "authenticated":
        return HTMLResponse(content=ADMIN_DASHBOARD)
    return HTMLResponse(content=ADMIN_LOGIN)

@app.post("/admindashboard/login", include_in_schema=False)
async def admin_login_submit(request: Request, username: str = Form(...), password: str = Form(...)):
    db = await get_db(request)
    await db_seed_if_empty(db)
    user_dict = await db_get_user_by_username(db, username)
    if user_dict and verify_password(password, user_dict["password_hash"]) and user_dict.get("role") == "admin":
        response = RedirectResponse(url="/admindashboard", status_code=status.HTTP_303_SEE_OTHER)
        response.set_cookie(key="admin_session", value="authenticated", httponly=True)
        return response
    return RedirectResponse(url="https://www.youtube.com", status_code=status.HTTP_303_SEE_OTHER)

@app.get("/admindashboard/logout", include_in_schema=False)
async def admin_logout():
    response = RedirectResponse(url="/admindashboard", status_code=status.HTTP_303_SEE_OTHER)
    response.delete_cookie("admin_session")
    return response

@app.get("/register", response_class=HTMLResponse, include_in_schema=False)
async def register_page():
    return HTMLResponse(content=REGISTER_PAGE)

@app.post("/register", tags=["Auth"])
async def register_user(request: Request, username: str = Form(...), email: str = Form(...), password: str = Form(...)):
    db = await get_db(request)
    # Check if username already exists
    existing = await db_get_user_by_username(db, username)
    if existing:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Username already exists")
    # Check if email already exists
    if db is not None:
        result = await db.prepare("SELECT id FROM users WHERE email = ?").bind(email).all()
        if result.get("results"):
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Email already registered")
    # Create the user
    user = await db_create_user(db, username, email, password, role="user")
    return {"status": "success", "message": f"User '{username}' registered successfully", "user": user}

@app.get("/ws-client", response_class=HTMLResponse, include_in_schema=False)
async def ws_client_page():
    return HTMLResponse(content=WS_CLIENT_PAGE)

@app.get("/api-explorer", response_class=HTMLResponse, include_in_schema=False)
async def api_explorer_page():
    return HTMLResponse(content=API_EXPLORER_PAGE)

@app.get("/token-generator", response_class=HTMLResponse, include_in_schema=False)
async def token_page():
    return HTMLResponse(content=TOKEN_PAGE)

@app.get("/uuid-generator", response_class=HTMLResponse, include_in_schema=False)
async def uuid_page():
    return HTMLResponse(content=UUID_PAGE)

@app.get("/item-lookup", response_class=HTMLResponse, include_in_schema=False)
async def item_page():
    return HTMLResponse(content=ITEM_PAGE)

@app.get("/github-info", response_class=HTMLResponse, include_in_schema=False)
async def github_page():
    return HTMLResponse(content=GITHUB_PAGE)

# ═══════════════════════════════════════════════════════════════════════════════
# WEBSOCKET ENDPOINTS
# ═══════════════════════════════════════════════════════════════════════════════

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

# ═══════════════════════════════════════════════════════════════════════════════
# CLOUDFLARE WORKERS ENTRY POINT
# ═══════════════════════════════════════════════════════════════════════════════
from workers import asgi
Default = asgi.entrypoint(app)
