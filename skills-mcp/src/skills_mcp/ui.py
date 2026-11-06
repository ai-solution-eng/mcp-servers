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
    for header in _IDENTITY_HEADERS:
        value = request.headers.get(header)
        if value and value.strip():
            return value.strip()
    return None


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
    rows = []
    for skill in visibility_mod.visible_skills(identity):
        name = skill.name
        active = " active" if name == selected else ""
        badge = f"<span class=badge>{len(skill.files)}</span>" if skill.files else ""
        rows.append(
            f"<a class='row{active}' href='/skills/{html.escape(name)}' "
            f"data-name='{html.escape(name)}'>{_FOLDER_ICON}"
            f"<span class=name>{html.escape(name)}</span>{badge}</a>"
        )
    who = (
        f"<span class=who>{html.escape(identity)}</span>"
        if identity
        else "<span class='who anon'>anonymous</span>"
    )
    return (
        "<div class=page-head>"
        f"<h1>skills</h1><span class=count>{len(rows)} skills</span>"
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

    inset = (
        f"<div class=crumb><a href='/'>all skills</a><span class=sep></span>"
        f"{html.escape(skill.name)}</div>"
        f"<h2>{html.escape(skill.name)}</h2>"
        f"<p class=desc>{html.escape(skill.description)}</p>"
        "<div class=actions>"
        f"<a class=btn href='/skills/{html.escape(skill.name)}/raw'>raw markdown</a>"
        "<button class=btn id=copy>copy markdown</button></div>"
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


def build_ui_routes() -> list:
    """The browser routes, or [] when SKILLS_UI_ENABLED=0."""
    if not ui_enabled():
        return []
    return [
        Route("/", index),
        Route("/skills/{name}", skill_detail),
        Route("/skills/{name}/raw", skill_raw),
    ]
