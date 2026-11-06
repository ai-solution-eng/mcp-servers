"""Read-only browser UI v1 — browse the skills catalogue.

Visual language borrowed from the fleet's agents-manage-ui (shadcn/Tailwind:
16rem sidebar, h-8 tree rows, muted-foreground icons, 0.625rem radius) but
re-implemented as plain CSS + server-rendered HTML. No React, no build step,
no new dependency — the same approach mm-rag and the logsearch console take.

  GET /                     shell + skill tree + first skill
  GET /skills/{name}        same shell, that skill selected
  GET /skills/{name}/raw    text/markdown, for copy/paste

Identity is read from the SSO proxy headers for DISPLAY only (never for
authorization). Read-only: nothing here writes, mints, or filters. Filtering
arrives with visible_skills(identity) in P2.

Exposure: these routes are not behind the API key (a browser cannot send one).
Set SKILLS_UI_ENABLED=0 to remove them; put the host behind the SSO edge proxy
before exposing it beyond the cluster.
"""

from __future__ import annotations

import html
import os

from starlette.responses import HTMLResponse, PlainTextResponse
from starlette.routing import Route

from . import auth as auth_mod
from . import prefs as prefs_mod
from . import state as state_mod
from . import visibility as visibility_mod

ENV_UI_ENABLED = "SKILLS_UI_ENABLED"

#: Headers an enforcing proxy (oauth2-proxy / ezaf-gateway) stamps with the
#: authenticated user. Display only.
_IDENTITY_HEADERS = (
    "x-forwarded-user",
    "x-auth-request-user",
    "x-auth-request-preferred-username",
)


def ui_enabled() -> bool:
    raw = os.getenv(ENV_UI_ENABLED)
    if raw is None:
        return True
    return raw.strip().lower() not in ("0", "false", "no", "off", "")


def identity_of(request) -> str | None:
    """Who is this request? Delegates to auth.resolve, so the UI accepts the
    same credentials the MCP does (SSO header when trusted, or an API key)."""
    try:
        found = auth_mod.resolve(request.scope)
    except Exception:  # noqa: BLE001 - the UI must render even if auth misbehaves
        return None
    return found.name if found and found.name else None


# ---------------------------------------------------------------------------
# Design tokens — converted from agents-manage-ui globals.css (dark theme)
# ---------------------------------------------------------------------------

_CSS = """
:root{
  --bg:#0b0b0f; --surface:#17171c; --surface-2:#1e1e24;
  --border:rgba(255,255,255,.10); --fg:#fafafa; --muted:#a1a1aa;
  --primary:#69a3ff; --primary-dim:rgba(105,163,255,.14);
  --radius:.625rem; --radius-lg:14px;
  --sidebar-w:16rem; --sidebar-w-collapsed:3rem;
  --row-h:2rem;
}
*{box-sizing:border-box}
html,body{height:100%}
body{margin:0;background:var(--bg);color:var(--fg);
  font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Inter,Helvetica,Arial,sans-serif}
a{color:inherit;text-decoration:none}
.wrap{max-width:1240px;margin:0 auto;padding:24px}
.page-head{display:flex;align-items:baseline;gap:12px;margin-bottom:14px;flex-wrap:wrap}
.page-head h1{font-size:18px;font-weight:600;margin:0}
.page-head .count{color:var(--muted);font-size:13px}
.page-head .source{color:var(--muted);font-size:12px;margin-left:auto}
.who{font-size:12px;color:var(--muted);border:1px solid var(--border);
  border-radius:999px;padding:2px 10px;white-space:nowrap}
.who.anon{opacity:.55}
.role{font-size:12px;color:var(--primary);border:1px solid var(--border);border-radius:999px;padding:2px 10px}

/* the shell: bordered, rounded, sidebar + inset (borrowed from the fleet UI) */
.shell{display:flex;min-height:78vh;border:1px solid var(--border);
  border-radius:var(--radius-lg);overflow:hidden;background:var(--surface)}
.sidebar{width:var(--sidebar-w);flex:0 0 var(--sidebar-w);border-right:1px solid var(--border);
  display:flex;flex-direction:column;background:var(--bg);transition:flex-basis .15s,width .15s}
.shell.collapsed .sidebar{width:var(--sidebar-w-collapsed);flex-basis:var(--sidebar-w-collapsed)}
.sidebar-head{height:44px;display:flex;align-items:center;gap:8px;padding:0 10px;
  border-bottom:1px solid var(--border);color:var(--muted);font-size:13px}
.shell.collapsed .sidebar-head .label{display:none}
.icon-btn{background:none;border:0;color:var(--muted);cursor:pointer;padding:4px;border-radius:6px;
  display:inline-flex;align-items:center;justify-content:center}
.icon-btn:hover{background:var(--surface-2);color:var(--fg)}
.search{margin:8px;padding:6px 10px;border-radius:calc(var(--radius) - 2px);
  border:1px solid var(--border);background:var(--surface);color:var(--fg);font-size:13px}
.shell.collapsed .search{display:none}
.tree{overflow:auto;padding:4px 6px 12px;flex:1}
.row{display:flex;align-items:center;gap:8px;height:var(--row-h);padding:0 8px;
  border-radius:calc(var(--radius) - 2px);color:var(--muted);font-size:13px;white-space:nowrap}
.row:hover{background:var(--surface-2);color:var(--fg)}
.row.active{background:var(--primary-dim);color:var(--primary)}
.row .name{overflow:hidden;text-overflow:ellipsis}
.row .badge{margin-left:auto;font-size:11px;color:var(--muted);opacity:.8}
.shell.collapsed .row .name,.shell.collapsed .row .badge{display:none}
.shell.collapsed .row{justify-content:center;padding:0}

.inset{flex:1;min-width:0;padding:22px 26px;overflow:auto}
.crumb{color:var(--muted);font-size:12px;margin-bottom:10px}
.crumb a:hover{color:var(--fg)}
.crumb .sep::after{content:"/";margin:0 6px;opacity:.5}
h2{font-size:20px;margin:0 0 6px;font-weight:600}
.desc{color:var(--muted);margin:0 0 16px;max-width:70ch}
.actions{display:flex;gap:8px;margin-bottom:18px}
.btn{display:inline-flex;align-items:center;height:32px;padding:0 12px;border-radius:calc(var(--radius) - 2px);
  border:1px solid var(--border);background:var(--surface);color:var(--fg);font-size:13px;cursor:pointer}
.btn:hover{border-color:var(--primary);color:var(--primary)}
h3.sec{font-size:12px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);
  margin:20px 0 8px;font-weight:600}
ul.files{list-style:none;margin:0 0 6px;padding:0;border:1px solid var(--border);
  border-radius:var(--radius);overflow:hidden}
ul.files li{padding:7px 12px;font:12px/1.4 ui-monospace,SFMono-Regular,Menlo,monospace;
  color:var(--muted);border-top:1px solid var(--border)}
ul.files li:first-child{border-top:0}
pre.body{background:var(--bg);border:1px solid var(--border);border-radius:var(--radius);
  padding:16px;overflow:auto;white-space:pre-wrap;word-wrap:break-word;
  font:12.5px/1.65 ui-monospace,SFMono-Regular,Menlo,monospace;color:#d4d4d8;margin:0}
.empty{color:var(--muted);font-size:13px}
.banner{border:1px solid var(--border);background:var(--surface-2);color:var(--muted);
  border-radius:var(--radius);padding:10px 14px;font-size:13px;margin-bottom:16px}
.row.off .name{opacity:.45;text-decoration:line-through}
.row.off .badge{display:inline}
.badge.off{color:var(--muted)}
.toggle{display:inline-flex;align-items:center;gap:8px;height:32px;padding:0 12px;
  border-radius:calc(var(--radius) - 2px);border:1px solid var(--border);
  background:var(--surface);color:var(--fg);font-size:13px;cursor:pointer}
.toggle:hover{border-color:var(--primary);color:var(--primary)}
.panel{border:1px solid var(--border);border-radius:var(--radius);padding:16px;margin:0 0 18px;background:var(--surface)}
.panel h3{margin:0 0 6px;font-size:14px}
.panel p{margin:0 0 12px;color:var(--muted);font-size:13px}
code.key{display:block;background:var(--bg);border:1px solid var(--primary);border-radius:8px;
  padding:10px 12px;font:12px ui-monospace,Menlo,monospace;color:var(--primary);
  word-break:break-all;margin:10px 0 6px}
.warn{color:var(--primary);font-size:12px}
"""

_FOLDER_ICON = (
    "<svg width=16 height=16 viewBox='0 0 24 24' fill='none' stroke='currentColor' "
    "stroke-width=2 stroke-linecap=round stroke-linejoin=round aria-hidden=true>"
    "<path d='M4 20h16a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.9a2 2 0 0 1-1.7-.9l-.8-1.2A2 2 0 0 0 8.9 3H4a2 2 0 0 0-2 2v13c0 1.1.9 2 2 2Z'/></svg>"
)
_PANEL_ICON = (
    "<svg width=16 height=16 viewBox='0 0 24 24' fill='none' stroke='currentColor' "
    "stroke-width=2 stroke-linecap=round stroke-linejoin=round aria-hidden=true>"
    "<rect x=3 y=3 width=18 height=18 rx=2/><path d='M9 3v18'/></svg>"
)


def _page(title: str, body: str, identity: str | None, status_code: int = 200) -> HTMLResponse:
    who = (
        f"<span class=who>{html.escape(identity)}</span>"
        if identity
        else "<span class='who anon'>anonymous</span>"
    )
    return HTMLResponse(
        "<!doctype html><html lang=en><head><meta charset=utf-8>"
        "<meta name=viewport content='width=device-width,initial-scale=1'>"
        f"<title>{html.escape(title)}</title><style>{_CSS}</style></head><body>"
        f"<div class=wrap>{body}</div></body></html>",
        status_code=status_code,
    )


def _source_line(state) -> str:
    source = state.source
    bits = [html.escape(source.origin)]
    if source.ref:
        bits.append(html.escape(source.ref))
    if source.commit:
        bits.append(html.escape(source.commit[:12]))
    if source.refreshed_at:
        bits.append(html.escape(source.refreshed_at))
    return " &middot; ".join(bits)


def _shell(state, identity: str | None, selected: str | None, inset: str) -> str:
    hidden = prefs_mod.disabled(identity)
    all_skills = visibility_mod.visible_skills(identity)
    private_names = {s.name for s in visibility_mod.private_skills(identity)}
    # disabled skills are not "visible" for tools, but the UI still lists them
    # so their owner can switch them back on.
    listed = list(all_skills)
    for name in sorted(hidden):
        skill = state.catalog.get(name)
        if skill is not None and name not in {s.name for s in listed}:
            listed.append(skill)
    listed.sort(key=lambda s: s.name)

    rows = []
    for skill in listed:
        name = skill.name
        classes = "row"
        if name == selected:
            classes += " active"
        if name in hidden:
            classes += " off"
        if name in private_names:
            badge = "<span class='badge off'>private</span>"
        elif skill.files:
            badge = f"<span class=badge>{len(skill.files)}</span>"
        else:
            badge = ""
        rows.append(
            f"<a class='{classes}' href='/skills/{html.escape(name)}' "
            f"data-name='{html.escape(name)}'>{_FOLDER_ICON}"
            f"<span class=name>{html.escape(name)}</span>{badge}</a>"
        )
    if identity:
        who = f"<a class=who href='/me'>{html.escape(identity)}</a>"
    else:
        who = "<span class='who anon'>anonymous</span>"
    return (
        "<div class=page-head>"
        f"<h1><a href='/'>skills</a></h1><span class=count>{len(listed)} skills</span>"
        f"<span class=source>{_source_line(state)}</span>{who}</div>"
        "<div class=shell id=shell>"
        "<aside class=sidebar>"
        "<div class=sidebar-head>"
        "<button class=icon-btn id=toggle title='collapse'>" + _PANEL_ICON + "</button>"
        "<span class=label>Files</span></div>"
        "<input class=search id=q placeholder='filter…' autocomplete=off>"
        "<nav class=tree id=tree>" + "".join(rows) + "</nav>"
        "</aside>"
        f"<main class=inset>{inset}</main>"
        "</div>"
        "<script>"
        "const shell=document.getElementById('shell');"
        "if(localStorage.getItem('ui.collapsed')==='1')shell.classList.add('collapsed');"
        "document.getElementById('toggle').onclick=()=>{shell.classList.toggle('collapsed');"
        "localStorage.setItem('ui.collapsed',shell.classList.contains('collapsed')?'1':'0')};"
        "const q=document.getElementById('q'),rows=[...document.querySelectorAll('.row')];"
        "q.addEventListener('input',()=>{const v=q.value.toLowerCase();"
        "rows.forEach(r=>{r.style.display=r.dataset.name.includes(v)?'':'none'})});"
        "</script>"
    )


async def index(request):
    state = state_mod.current_state()
    identity = identity_of(request)
    visible = visibility_mod.visible_skills(identity)
    if visible:
        from starlette.responses import RedirectResponse

        return RedirectResponse(f"/skills/{visible[0].name}", status_code=307)
    inset = "<p class=empty>No skills are visible to you. Check /admin/status.</p>"
    return _page("skills", _shell(state, identity, None, inset), identity)


async def skill_detail(request):
    name = request.path_params.get("name", "")
    state = state_mod.current_state()
    identity = identity_of(request)
    skill = visibility_mod.get_visible(name, identity)
    if skill is None and identity and name in prefs_mod.disabled(identity):
        # Hidden for its owner, but still theirs to switch back on.
        skill = state.catalog.get(name)
    if skill is None:
        inset = (
            f"<h2>Unknown skill</h2><p class=desc>{html.escape(name)} is not in the catalogue.</p>"
            "<a class=btn href='/'>back to skills</a>"
        )
        return _page("not found", _shell(state, identity, None, inset), identity, status_code=404)

    files = ""
    if skill.files:
        items = "".join(f"<li>{html.escape(path)}</li>" for path in skill.files)
        files = f"<h3 class=sec>Files</h3><ul class=files>{items}</ul>"

    hidden = identity is not None and skill.name in prefs_mod.disabled(identity)
    if identity is None:
        toggle = (
            "<div class=banner>You are browsing anonymously. "
            "Sign in (or use your token) to enable/disable skills for yourself.</div>"
        )
    else:
        label = "Enable for me" if hidden else "Disable for me"
        toggle = (
            f"<form method=post action='/skills/{html.escape(skill.name)}/toggle' style='display:inline'>"
            f"<button class=toggle type=submit>{label}</button></form>"
        )
    state_note = (
        "<div class=banner>Disabled for you — your agent will not see this skill.</div>"
        if hidden
        else ""
    )
    inset = (
        f"<div class=crumb><a href='/'>all skills</a><span class=sep></span>"
        f"{html.escape(skill.name)}</div>"
        f"<h2>{html.escape(skill.name)}</h2>"
        f"<p class=desc>{html.escape(skill.description)}</p>"
        f"{state_note}"
        "<div class=actions>"
        f"<a class=btn href='/skills/{html.escape(skill.name)}/raw'>raw markdown</a>"
        "<button class=btn id=copy>copy markdown</button>"
        f"{toggle}</div>"
        f"{files}"
        f"<h3 class=sec>Body</h3><pre class=body id=body>{html.escape(skill.body)}</pre>"
        "<script>document.getElementById('copy').onclick=async()=>{"
        "await navigator.clipboard.writeText(document.getElementById('body').innerText);"
        "const b=document.getElementById('copy');b.textContent='copied';"
        "setTimeout(()=>b.textContent='copy markdown',1200)};</script>"
    )
    return _page(skill.name, _shell(state, identity, skill.name, inset), identity)


async def skill_raw(request):
    name = request.path_params.get("name", "")
    skill = visibility_mod.get_visible(name, identity_of(request))
    if skill is None:
        return PlainTextResponse("unknown skill: " + name, status_code=404)
    return PlainTextResponse(skill.body, media_type="text/markdown; charset=utf-8")


async def skill_toggle(request):
    """Flip this skill on/off for the caller. Form post → redirect back."""
    from starlette.responses import RedirectResponse

    name = request.path_params.get("name", "")
    identity = identity_of(request)
    if identity is None:
        return _page(
            "sign in required",
            "<h1>Sign in required</h1><p class=desc>Personal settings need an identity "
            "(SSO header or your API key).</p><a class=btn href='/'>back</a>",
            None,
            status_code=401,
        )
    if visibility_mod.get_visible(name, identity) is None and name not in prefs_mod.disabled(identity):
        return _page("unknown skill", f"<h1>Unknown skill</h1><p class=desc>{html.escape(name)}</p>", identity, status_code=404)
    currently_hidden = name in prefs_mod.disabled(identity)
    prefs_mod.set_disabled(identity, name, not currently_hidden)
    return RedirectResponse(f"/skills/{name}", status_code=303)


async def me_page(request):
    state = state_mod.current_state()
    identity = identity_of(request)
    if identity is None:
        body = (
            "<h2>You</h2>"
            "<div class=banner>Anonymous. The UI personalizes only when the request "
            "carries an identity — an SSO header from the proxy, or an API key.</div>"
            "<a class=btn href='/'>back to skills</a>"
        )
        return _page("you", _shell(state, None, None, body), None)

    token = prefs_mod.token_record(identity)
    hidden = sorted(prefs_mod.disabled(identity))
    private = visibility_mod.private_skills(identity)

    if token:
        token_panel = (
            "<div class=panel><h3>Your token</h3>"
            f"<p>Prefix <code>{html.escape(str(token.get('prefix')))}</code> · "
            f"created {html.escape(str(token.get('created_at')))}</p>"
            "<form method=post action='/me/token/rotate'>"
            "<button class=toggle type=submit>Rotate token</button></form>"
            "<p class=warn>Rotating invalidates the old token immediately.</p></div>"
        )
    else:
        token_ok = identity not in auth_mod.env_managed_names()
        if token_ok:
            token_panel = (
                "<div class=panel><h3>Mint your token</h3>"
                "<p>One token per person. It carries exactly your current access and is "
                "shown once.</p>"
                "<form method=post action='/me/token'>"
                "<button class=toggle type=submit>Mint my token</button></form></div>"
            )
        else:
            token_panel = (
                "<div class=panel><h3>Your token</h3>"
                "<p>This identity is managed by the operator registry — ask an operator "
                "to rotate your key.</p></div>"
            )

    hidden_block = (
        "<div class=panel><h3>Disabled for you (" + str(len(hidden)) + ")</h3><p>"
        + ", ".join(f"<a href='/skills/{html.escape(n)}'>{html.escape(n)}</a>" for n in hidden)
        + "</p></div>"
        if hidden
        else "<div class=panel><h3>Disabled for you</h3><p>Nothing disabled — you see every global skill.</p></div>"
    )
    private_block = (
        "<div class=panel><h3>Private skills (" + str(len(private)) + ")</h3><p>"
        + ", ".join(f"<a href='/skills/{html.escape(s.name)}'>{html.escape(s.name)}</a>" for s in private)
        + "</p><p class=warn>Private skills live only on this cluster, never in git.</p></div>"
        if private
        else "<div class=panel><h3>Private skills</h3><p>None yet. Private skills are stored "
             "per person and are invisible to everyone else.</p></div>"
    )

    body = (
        f"<h2>{html.escape(identity)}</h2>"
        f"<p class=desc>Visible to you: {len(visibility_mod.visible_skills(identity))} skills</p>"
        f"{token_panel}{hidden_block}{private_block}"
        "<a class=btn href='/'>back to skills</a>"
    )
    return _page("you", _shell(state, identity, None, body), identity)


def _key_page(request, identity: str, key: str, *, rotated: bool):
    state = state_mod.current_state()
    verb = "rotated" if rotated else "minted"
    body = (
        f"<h2>Token {verb}</h2>"
        "<p class=desc>This is the only time the full token is shown. Store it now.</p>"
        f"<code class=key>{html.escape(key)}</code>"
        "<div class=banner>Configure your agent with it:<br>"
        f"<code>X-API-Key: {html.escape(key[:14])}…</code> — or "
        "<code>Authorization: Bearer …</code></div>"
        "<a class=btn href='/me'>back to you</a>"
    )
    return _page("token", _shell(state, identity, None, body), identity)


async def me_token(request):
    identity = identity_of(request)
    if identity is None:
        return _page("sign in required", "<h1>Sign in required</h1>", None, status_code=401)
    if identity in auth_mod.env_managed_names():
        return _page(
            "operator-managed",
            f"<h1>Operator-managed identity</h1><p class=desc>{html.escape(identity)} is in the "
            "operator key registry; an operator must rotate that key.</p><a class=btn href='/me'>back</a>",
            identity,
            status_code=409,
        )
    if prefs_mod.token_record(identity):
        return _page(
            "token exists",
            "<h1>A token already exists</h1><p class=desc>Rotate it instead.</p>"
            "<a class=btn href='/me'>back</a>",
            identity,
            status_code=409,
        )
    key = auth_mod.mint(identity)
    return _key_page(request, identity, key, rotated=False)


async def me_token_rotate(request):
    identity = identity_of(request)
    if identity is None:
        return _page("sign in required", "<h1>Sign in required</h1>", None, status_code=401)
    if identity in auth_mod.env_managed_names():
        return _page("operator-managed", "<h1>Operator-managed identity</h1>", identity, status_code=409)
    key = auth_mod.mint(identity)
    return _key_page(request, identity, key, rotated=True)


def build_ui_routes() -> list:
    """The browser routes, or [] when SKILLS_UI_ENABLED=0."""
    if not ui_enabled():
        return []
    return [
        Route("/", index),
        Route("/me", me_page),
        Route("/me/token", me_token, methods=["POST"]),
        Route("/me/token/rotate", me_token_rotate, methods=["POST"]),
        Route("/skills/{name}", skill_detail),
        Route("/skills/{name}/toggle", skill_toggle, methods=["POST"]),
        Route("/skills/{name}/raw", skill_raw),
    ]
