"""Web interface + HTTP MCP endpoint in one service.

    /             session status, Check / Renew / Log in buttons
    /login        live view of the server-side login browser (works on a phone)
    /recipes      recipe search + detail pages; /favourites (see recipe_pages.py)
    /mcp          the MCP server over streamable HTTP (Bearer token auth)

Everything requires MC_WEB_PASSWORD: browsers use HTTP Basic (any username), MCP
clients send `Authorization: Bearer <password>`.

Run: uv run monsieur-cuisine-mcp-web [--host 0.0.0.0] [--port 8090]
"""
import argparse
import asyncio
import base64
import contextlib
import hashlib
import hmac
import html
import json
import logging
import os
import time

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Mount, Route, WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

from .. import catalogue
from ..config import MC_CATALOGUE_SYNC_INTERVAL_SECONDS, MC_WEB_CHECK_INTERVAL_SECONDS
from ..logging_config import setup_logging
from . import recipe_pages, session
from .remote_login import RemoteLogin, has_saved_credentials

AUTO_LOGIN_MIN_INTERVAL = 12 * 3600  # background auto-login at most this often

logger = logging.getLogger(__name__)

WS_TOKEN_TTL = 15 * 60


# ------------------------------------------------------------------ auth
def _password() -> str:
    return os.environ.get("MC_WEB_PASSWORD", "")


def sign_ws_token(secret: str, now: float | None = None) -> str:
    exp = int((now or time.time()) + WS_TOKEN_TTL)
    sig = hmac.new(secret.encode(), f"ws:{exp}".encode(), hashlib.sha256).hexdigest()
    return f"{exp}.{sig}"


def verify_ws_token(secret: str, token: str, now: float | None = None) -> bool:
    try:
        exp_s, sig = token.split(".", 1)
        exp = int(exp_s)
    except ValueError:
        return False
    good = hmac.new(secret.encode(), f"ws:{exp}".encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig, good) and exp >= (now or time.time())


def credentials_ok(secret: str, authorization: str) -> bool:
    if not secret or not authorization:
        return False
    scheme, _, value = authorization.partition(" ")
    if scheme.lower() == "bearer":
        return hmac.compare_digest(value.strip().encode(), secret.encode())
    if scheme.lower() == "basic":
        try:
            _, _, pw = base64.b64decode(value.strip()).decode().partition(":")
        except (ValueError, UnicodeDecodeError):
            return False
        return hmac.compare_digest(pw.encode(), secret.encode())
    return False


class AuthMiddleware:
    """Pure-ASGI guard for HTTP (Basic/Bearer) and WebSocket (signed token) traffic."""

    def __init__(self, app, secret: str) -> None:
        self.app, self.secret = app, secret

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            return await self.app(scope, receive, send)
        headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
        if scope["type"] == "websocket":
            from urllib.parse import parse_qs
            token = parse_qs(scope.get("query_string", b"").decode()).get("token", [""])[0]
            if verify_ws_token(self.secret, token) or credentials_ok(self.secret, headers.get("authorization", "")):
                return await self.app(scope, receive, send)
            await send({"type": "websocket.close", "code": 4401})
            return
        if credentials_ok(self.secret, headers.get("authorization", "")):
            return await self.app(scope, receive, send)
        is_mcp = scope.get("path", "").startswith("/mcp")
        resp = Response("Authentication required", status_code=401,
                        headers={} if is_mcp else {"WWW-Authenticate": 'Basic realm="Monsieur Cuisine MCP"'})
        await resp(scope, receive, send)


# ------------------------------------------------------------------ pages
CSS = """
:root{--bg:#f6f7f9;--fg:#1c1e21;--card:#fff;--muted:#6b7280;--line:#e5e7eb;--acc:#2563eb;--ok:#065f46;--okbg:#d1fae5;--bad:#991b1b;--badbg:#fee2e2}
@media(prefers-color-scheme:dark){:root{--bg:#111318;--fg:#e6e8eb;--card:#1b1e25;--muted:#9aa3af;--line:#2b303a;--acc:#60a5fa;--okbg:#064e3b;--ok:#a7f3d0;--badbg:#450a0a;--bad:#fecaca}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.45 system-ui,sans-serif}
main{max-width:760px;margin:0 auto;padding:16px}h1{font-size:1.2rem;margin:4px 0 14px}h2{font-size:1rem;margin:0 0 8px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 14px;margin-bottom:12px}
.muted{color:var(--muted);font-size:.85rem}.row{display:flex;gap:8px;flex-wrap:wrap;align-items:center;justify-content:space-between}
.pill{display:inline-block;padding:2px 10px;border-radius:99px;font-weight:600;font-size:.8rem}
.ok{background:var(--okbg);color:var(--ok)}.bad{background:var(--badbg);color:var(--bad)}.unk{background:var(--line);color:var(--fg)}
button,.btn{font:inherit;padding:10px 14px;border-radius:8px;border:0;background:var(--acc);color:#fff;font-weight:600;cursor:pointer;text-decoration:none;display:inline-block}
button.alt{background:#4b5563}button.warn{background:#b91c1c}form{display:inline}
table{border-collapse:collapse;width:100%;font-size:.9rem}td{padding:3px 0;vertical-align:top}td:first-child{color:var(--muted);width:40%}
code{font-size:.85em}
"""


def page(title: str, body: str, head: str = "") -> HTMLResponse:
    return HTMLResponse(f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{html.escape(title)}</title>
<style>{CSS}</style>{head}</head><body><main><h1>🍲 Monsieur Cuisine MCP</h1>{body}</main></body></html>""")


def pill(ok: bool | None, yes: str, no: str, unknown: str = "Unknown") -> str:
    if ok is None:
        return f'<span class="pill unk">{unknown}</span>'
    return f'<span class="pill {"ok" if ok else "bad"}">{yes if ok else no}</span>'


def fmt_event(ev: dict | None) -> str:
    if not ev:
        return '<span class="muted">never</span>'
    return f'{pill(ev["ok"], "OK", "Failed")} <span class="muted">{html.escape(ev["at"][:16].replace("T", " "))} UTC</span>' \
           f'<br><span class="muted">{html.escape(ev["message"])}</span>'


async def index(request: Request) -> HTMLResponse:
    s = session.session_summary()
    st = session.load_status()
    check = st.get("check")
    healthy = check["ok"] if check else None
    if not s["saved"]:
        headline = pill(False, "", "Not logged in")
    else:
        headline = pill(healthy, "Logged in &amp; working", "Session not working", "Logged in (not checked yet)")
    mc = ", ".join(f'{html.escape(c["name"][:28])} (expires {c["expires"] or "with browser session"})'
                   for c in s.get("mc_cookies", [])) or "none"
    login: RemoteLogin = request.app.state.login
    body = f"""{recipe_pages.NAV}
<div class="card"><div class="row"><h2>Session</h2>{headline}</div>
<table>
<tr><td>Saved session</td><td>{"yes, " + html.escape((s.get("saved_at") or "")[:16].replace("T", " ")) + " UTC" if s["saved"] else "no"}</td></tr>
<tr><td>Monsieur Cuisine cookie</td><td>{mc if s["saved"] else "–"}</td></tr>
<tr><td>Lidl sign-in cookies</td><td>{s.get("sso_cookie_count", 0)}{(" (latest expiry " + html.escape(s["sso_latest_expiry"]) + ")") if s.get("sso_latest_expiry") else ""}</td></tr>
<tr><td>Last check</td><td>{fmt_event(check)}</td></tr>
<tr><td>Last renewal</td><td>{fmt_event(st.get("renew"))}</td></tr>
<tr><td>Last login</td><td>{fmt_event(st.get("login"))}</td></tr>
</table></div>
<div class="card row" style="justify-content:flex-start">
<form method="post" action="/check"><button class="alt">Check now</button></form>
<form method="post" action="/renew"><button class="alt">Renew (no password)</button></form>
{'<a class="btn" href="/login">Login in progress…</a>' if login.running else
  '<form method="post" action="/login/go"><button>Log in</button></form>'}
</div>
{catalogue_card()}
<p class="muted">Checks run every {MC_WEB_CHECK_INTERVAL_SECONDS // 3600}h and renew automatically when the session stops working{
"; if renewal fails it signs in again with your saved details (at most every 12h)" if has_saved_credentials() and auto_login_enabled() else ""}.
{"Log in signs in automatically with your saved details and shows you the browser only if it needs help." if has_saved_credentials() else
 "Add MC_LIDL_EMAIL / MC_LIDL_PASSWORD to ~/.config/mc-mcp.env for one-tap login."}
MCP endpoint: <code>{html.escape(str(request.base_url))}mcp</code>{" (Bearer = this site's password)" if request.app.state.auth else " (no password)"}.</p>"""
    return page("Monsieur Cuisine MCP", body)


def catalogue_card() -> str:
    st = catalogue.stats()
    p = catalogue.progress
    last = st.get("last_sync")
    if p.running:
        state = (f'<span class="pill unk">Syncing: {html.escape(p.phase)} {p.done}/{p.total}</span>'
                 if p.phase == "fetching" else f'<span class="pill unk">Syncing: {html.escape(p.phase)}…</span>')
    elif st["recipes"]:
        state = pill(True, f'{st["recipes"]} recipes', "")
    else:
        state = pill(False, "", "Empty")
    detail = (f'Last sync {html.escape(last["finished"][:16].replace("T", " "))} UTC: {last["listed"]} listed, '
              f'{last["fetched"]} fetched, {last["removed"]} removed, {last["errors"]} errors.' if last else "Never synced.")
    button = "" if p.running else '<form method="post" action="/catalogue/sync"><button class="alt">Sync now</button></form>'
    return f"""<div class="card"><div class="row"><h2>Recipe catalogue (Smart, local copy)</h2>{state}</div>
<p class="muted" style="margin:6px 0">{detail} {st["distinct_ingredients"]} distinct ingredients. Syncs nightly; the first sync
takes ~20 minutes (one request per second).</p>{button}</div>"""


async def catalogue_sync_now(request: Request) -> RedirectResponse:
    if not catalogue.progress.running:
        asyncio.create_task(catalogue.sync())
        await asyncio.sleep(0.2)
    return RedirectResponse("/", status_code=303)


async def do_check(request: Request) -> RedirectResponse:
    await session.check_session()
    return RedirectResponse("/", status_code=303)


async def do_renew(request: Request) -> RedirectResponse:
    result = await session.renew_session()
    if result["ok"]:
        await session.check_session()
    return RedirectResponse("/", status_code=303)


async def status_json(request: Request) -> JSONResponse:
    login: RemoteLogin = request.app.state.login
    return JSONResponse({"session": session.session_summary(), "events": session.load_status(),
                         "login": login.info(), "catalogue": {**catalogue.stats(), "progress": catalogue.progress.as_dict()}})


LOGIN_JS = r"""
const $=s=>document.querySelector(s), img=$('#screen'), st=$('#state'), url=$('#url'), kb=$('#kb');
let ws, state='idle';
function connect(){
  ws=new WebSocket((location.protocol==='https:'?'wss://':'ws://')+location.host+'/login/ws?token='+TOKEN);
  ws.onmessage=e=>{const m=JSON.parse(e.data);
    if(m.type==='frame'){img.src='data:image/jpeg;base64,'+m.data;}
    else if(m.type==='status'){state=m.state; st.textContent=m.message||m.state; st.className='pill '+(m.state==='success'?'ok':m.state==='failed'||m.state==='cancelled'?'bad':'unk');
      url.textContent=m.url?new URL(m.url).host:''; $('#start').hidden=['starting','auto','waiting'].includes(m.state); $('#cancel').hidden=!$('#start').hidden;
      if(m.state==='success') setTimeout(()=>location.href='/',2500);}};
  ws.onclose=()=>setTimeout(connect,2000);
}
function send(o){ if(ws&&ws.readyState===1) ws.send(JSON.stringify(o)); }
function norm(e){const r=img.getBoundingClientRect(); return {x:(e.clientX-r.left)/r.width, y:(e.clientY-r.top)/r.height};}
let down=null;
img.addEventListener('pointerdown',e=>{down={...norm(e),cy:e.clientY,t:Date.now()};});
img.addEventListener('pointerup',e=>{ if(!down) return; const dy=down.cy-e.clientY;
  if(Math.abs(dy)>15) send({type:'wheel',x:down.x,y:down.y,dy:dy*2}); else { send({type:'click',...norm(e)}); kb.focus({preventScroll:true}); }
  down=null;});
img.addEventListener('wheel',e=>{e.preventDefault(); send({type:'wheel',...norm(e),dy:e.deltaY});},{passive:false});
// Forward whatever lands in the box (typing, paste, password-manager autofill, dictation), then clear it.
kb.addEventListener('input',()=>{ if(kb.value){ send({type:'text',text:kb.value}); kb.value=''; } });
kb.addEventListener('beforeinput',e=>{
  if(e.inputType==='deleteContentBackward'&&!kb.value){ send({type:'key',key:'Backspace'}); e.preventDefault(); }
  else if(e.inputType==='insertLineBreak'){ send({type:'key',key:'Enter'}); e.preventDefault(); }});
kb.addEventListener('keydown',e=>{
  if(e.key==='Backspace'&&!kb.value){ send({type:'key',key:'Backspace'}); e.preventDefault(); }
  else if(['Enter','Tab','Escape','ArrowLeft','ArrowRight','ArrowUp','ArrowDown','Delete'].includes(e.key)){send({type:'key',key:e.key}); e.preventDefault();}});
document.querySelectorAll('[data-fill]').forEach(b=>b.onclick=()=>send({type:'fill',field:b.dataset.fill}));
document.querySelectorAll('[data-key]').forEach(b=>b.onclick=()=>{send({type:'key',key:b.dataset.key}); kb.focus({preventScroll:true});});
$('#back').onclick=()=>send({type:'back'}); $('#reload').onclick=()=>send({type:'reload'});
$('#start').onclick=()=>fetch('/login/start',{method:'POST',headers:{'Content-Type':'application/json'},
  body:JSON.stringify({width:Math.round(document.querySelector('main').clientWidth),height:Math.round(window.innerHeight*0.75)})});
$('#cancel').onclick=()=>fetch('/login/cancel',{method:'POST'});
connect();
"""


async def login_page(request: Request) -> HTMLResponse:
    token = sign_ws_token(request.app.state.secret)
    from .remote_login import saved_credential
    fill_buttons = "".join(f'<button data-fill="{f}">Insert {f}</button>' for f in ("email", "password")
                           if saved_credential(f))
    fill_hint = ('<p class="muted" style="margin:8px 0 0">Tap a field in the Lidl window first, then Insert.</p>'
                 if fill_buttons else "")
    body = f"""
<div class="card"><div class="row"><span id="state" class="pill unk">Connecting…</span><span class="muted" id="url"></span></div>
<p class="muted" style="margin:8px 0 0">This is a real Chrome running on the server. Tap fields, then type with the box below.
Nothing is typed for you, and your password isn't stored, only the resulting session.</p></div>
<div class="row" style="justify-content:flex-start;margin-bottom:8px">
<button id="start">{"Sign in automatically" if has_saved_credentials() else "Start login"}</button><button id="cancel" class="warn" hidden>Cancel</button>
<button id="back" class="alt">◀ Back</button><button id="reload" class="alt">↻ Reload</button><a class="btn" style="background:#4b5563" href="/">Status</a></div>
<img id="screen" alt="" style="width:100%;border:1px solid var(--line);border-radius:8px;background:#000;min-height:200px;touch-action:none;user-select:none;-webkit-user-select:none" draggable="false">
<div class="card" style="margin-top:8px"><input id="kb" autocomplete="off" autocapitalize="off" autocorrect="off" spellcheck="false"
 placeholder="Type here → goes into the selected field" style="width:100%;font:inherit;padding:10px;border-radius:8px;border:1px solid var(--line);background:var(--card);color:var(--fg)">
<div class="row" style="justify-content:flex-start;margin-top:8px">
<button class="alt" data-key="Backspace">⌫</button><button class="alt" data-key="Tab">Tab ⇥</button><button class="alt" data-key="Enter">Enter ↵</button>{fill_buttons}</div>
{fill_hint}</div>
<script>const TOKEN={json.dumps(token)};{LOGIN_JS}</script>"""
    return page("Log in – Monsieur Cuisine MCP", body)


async def login_start(request: Request) -> JSONResponse:
    try:
        body = await request.json()
    except ValueError:
        body = {}
    login: RemoteLogin = request.app.state.login
    login.start(int(body.get("width", 1024)), int(body.get("height", 800)), auto=bool(body.get("auto", True)))
    return JSONResponse(login.info())


async def login_go(request: Request) -> RedirectResponse:
    """Status-page Log in button: start (automatic if possible) and show the live view."""
    request.app.state.login.start(1024, 800, auto=True)
    return RedirectResponse("/login", status_code=303)


async def login_cancel(request: Request) -> JSONResponse:
    request.app.state.login.cancel()
    return JSONResponse({"ok": True})


async def login_ws(ws: WebSocket) -> None:
    await ws.accept()
    login: RemoteLogin = ws.app.state.login
    await login.add_viewer(ws)
    try:
        while True:
            msg = json.loads(await ws.receive_text())
            await login.handle_input(msg)
    except (WebSocketDisconnect, ValueError):
        pass
    finally:
        login.remove_viewer(ws)


async def healthz(request: Request) -> JSONResponse:
    return JSONResponse({"ok": True})


# ------------------------------------------------------------------ app
def auto_login_enabled() -> bool:
    return os.environ.get("MC_WEB_AUTO_LOGIN", "1") == "1"


def maybe_auto_login(login: RemoteLogin, now: float | None = None) -> bool:
    """After a failed check + renewal, sign in again unattended - conservatively:
    only with saved credentials, never while a login runs, at most every 12h."""
    status = session.load_status()
    if (status.get("check") or {}).get("ok") or login.running:
        return False
    if not (auto_login_enabled() and has_saved_credentials()):
        return False
    last = status.get("auto_login")
    if last:
        from datetime import datetime
        age = (now or time.time()) - datetime.fromisoformat(last["at"]).timestamp()
        if age < AUTO_LOGIN_MIN_INTERVAL:
            return False
    session.record("auto_login", True, "Session expired and renewal failed - started automatic sign-in.")
    login.start(1024, 800, auto=True)
    return True


def catalogue_sync_due(stats: dict, now: float | None = None) -> bool:
    last = stats.get("last_sync")
    if not stats.get("recipes") or not last:
        return True
    from datetime import datetime
    age = (now or time.time()) - datetime.fromisoformat(last["finished"]).timestamp()
    return age >= MC_CATALOGUE_SYNC_INTERVAL_SECONDS


async def _periodic_catalogue_sync() -> None:
    await asyncio.sleep(30)  # let the service settle after start-up
    while True:
        try:
            if catalogue_sync_due(catalogue.stats()):
                await catalogue.sync()
        except Exception:  # noqa: BLE001 - never let the loop die
            logger.exception("Catalogue sync failed")
        await asyncio.sleep(3600)


async def _periodic_check(login: RemoteLogin) -> None:
    while True:
        try:
            await session.check_and_heal()
            maybe_auto_login(login)
        except Exception:  # noqa: BLE001 - never let the loop die
            logger.exception("Periodic session check failed")
        await asyncio.sleep(MC_WEB_CHECK_INTERVAL_SECONDS)


def create_app(secret: str | None, *, with_mcp: bool = True, background: bool = True):
    """secret=None disables authentication entirely (MC_WEB_NO_AUTH=1)."""
    routes = [
        Route("/", index),
        Route("/check", do_check, methods=["POST"]),
        Route("/renew", do_renew, methods=["POST"]),
        Route("/status.json", status_json),
        Route("/catalogue/sync", catalogue_sync_now, methods=["POST"]),
        Route("/healthz", healthz),
        Route("/login", login_page),
        Route("/login/start", login_start, methods=["POST"]),
        Route("/login/go", login_go, methods=["POST"]),
        Route("/login/cancel", login_cancel, methods=["POST"]),
        WebSocketRoute("/login/ws", login_ws),
        Route("/recipes", recipe_pages.recipes_search),
        Route("/recipes/{recipe_id:int}", recipe_pages.recipe_detail),
        Route("/recipes/{recipe_id:int}/favourite", recipe_pages.recipe_favourite, methods=["POST"]),
        Route("/favourites", recipe_pages.favourites),
    ]
    mcp_app = None
    if with_mcp:
        from ..server import mcp
        mcp_app = mcp.http_app(path="/mcp")
        routes.append(Mount("/", app=mcp_app))

    @contextlib.asynccontextmanager
    async def lifespan(app):
        task = asyncio.create_task(_periodic_check(app.state.login)) if background else None
        sync_task = asyncio.create_task(_periodic_catalogue_sync()) if background else None
        try:
            if mcp_app is not None:
                async with mcp_app.lifespan(app):
                    yield
            else:
                yield
        finally:
            for t in (task, sync_task):
                if t:
                    t.cancel()
            app.state.login.cancel()

    app = Starlette(routes=routes, lifespan=lifespan)
    app.state.login = RemoteLogin()
    app.state.auth = secret is not None
    if secret is None:
        import secrets as _secrets
        app.state.secret = _secrets.token_urlsafe(32)  # only signs the page's WebSocket token
        return app
    app.state.secret = secret
    return AuthMiddleware(app, secret)


def main() -> None:
    parser = argparse.ArgumentParser(description="Monsieur Cuisine MCP web interface + HTTP MCP server")
    parser.add_argument("--host", default=os.environ.get("MC_WEB_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("MC_WEB_PORT", "8090")))
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()
    setup_logging(logging.DEBUG if args.debug else None)
    if os.environ.get("MC_WEB_NO_AUTH") == "1":
        secret = None
        logger.warning("Authentication DISABLED (MC_WEB_NO_AUTH=1): anyone who can reach %s:%s "
                       "can use your Lidl session.", args.host, args.port)
    else:
        secret = _password()
        if len(secret) < 8:
            raise SystemExit("Set MC_WEB_PASSWORD (8+ characters), or MC_WEB_NO_AUTH=1 to run without one.")
    uvicorn.run(create_app(secret), host=args.host, port=args.port, log_level="info", ws="websockets")


if __name__ == "__main__":
    main()
