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

import asyncio
import html
import os
import re
from datetime import datetime, timezone

from markdown_it import MarkdownIt
from starlette.responses import HTMLResponse, PlainTextResponse
from starlette.routing import Route

from . import auth as auth_mod
from . import prefs as prefs_mod
from . import private as private_mod
from . import refresh as refresh_mod
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


#: CommonMark with raw HTML DISABLED. Skills are content we render for other
#: people, and private ones are user-authored, so markup in the file must never
#: become markup in the page. markdown-it's default validateLink also refuses
#: javascript:/data: targets.
_MD = (
    MarkdownIt("commonmark", {"html": False, "linkify": False, "typographer": False})
    .enable("table")
    .enable("strikethrough")
)


def render_markdown(text: str) -> str:
    return _MD.render(text or "")


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
  color-scheme:dark;
}
[data-theme=light]{
  --bg:#ffffff; --surface:#fafafa; --surface-2:#f0f0f2;
  --border:rgba(0,0,0,.12); --fg:#17171b; --muted:#5c5c68;
  --primary:#2f6fed; --primary-dim:rgba(47,111,237,.12);
  color-scheme:light;
}
.theme{display:inline-flex;border:1px solid var(--border);border-radius:999px;overflow:hidden}
.theme button{background:none;border:0;color:var(--muted);font-size:12px;padding:4px 10px;cursor:pointer}
.theme button[aria-pressed=true]{background:var(--surface-2);color:var(--fg)}
.md{font-size:14px;line-height:1.65;max-width:78ch}
.md h1,.md h2,.md h3,.md h4{margin:22px 0 8px;line-height:1.35;font-weight:600;letter-spacing:-.01em}
.md>h1:first-child,.md>h2:first-child{margin-top:0}
/* the body's own #title sits UNDER the page title, so keep it smaller than an
   <h1> would be — the page name is the real heading */
.md h1{font-size:17px}.md h2{font-size:15px}.md h3{font-size:13.5px}
.md h3{color:var(--muted);text-transform:uppercase;letter-spacing:.04em}
.md p{margin:10px 0}
.md ul,.md ol{margin:10px 0 10px 22px}
.md li{margin:4px 0}
.md code{background:var(--surface-2);padding:2px 5px;border-radius:4px;font-size:12.5px}
.md pre{background:var(--surface-2);padding:12px;border-radius:8px;overflow:auto}
.md pre code{background:none;padding:0}
.md table{border-collapse:collapse;margin:12px 0;font-size:13px;display:block;overflow:auto}
.md th,.md td{border:1px solid var(--border);padding:6px 10px;text-align:left}
.md th{background:var(--surface-2)}
.md blockquote{border-left:3px solid var(--border);margin:12px 0;padding:2px 12px;color:var(--muted)}
.md a{color:var(--primary)}
.md hr{border:0;border-top:1px solid var(--border);margin:18px 0}
*{box-sizing:border-box}
html,body{height:100%}
body{margin:0;background:var(--bg);color:var(--fg);
  font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Inter,Helvetica,Arial,sans-serif}
a{color:inherit;text-decoration:none}
.wrap{max-width:1240px;margin:0 auto;padding:24px}
.page-head{display:flex;align-items:baseline;gap:12px;margin-bottom:14px;flex-wrap:wrap}
.page-head h1{font-size:18px;font-weight:600;margin:0}
.page-head .count{color:var(--muted);font-size:13px}
.page-head .source{color:var(--muted);font-size:12px}
.head-actions{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.pill{display:inline-flex;align-items:center;height:28px;padding:0 10px;border-radius:999px;
  border:1px solid var(--border);background:var(--surface);color:var(--fg);font-size:12px;
  text-decoration:none;cursor:pointer;white-space:nowrap}
.pill:hover{background:var(--surface-2);border-color:var(--muted)}
.pill.primary{background:var(--primary);border-color:var(--primary);color:#fff}
.pill.primary:hover{filter:brightness(1.08)}
.flash{border:1px solid var(--border);background:var(--surface-2);color:var(--fg);
  padding:8px 12px;border-radius:var(--radius);margin-bottom:12px;font-size:13px}
.who{font-size:12px;color:var(--muted);border:1px solid var(--border);
  border-radius:999px;padding:2px 10px;white-space:nowrap}
.who.anon{opacity:.55}
.role{font-size:12px;color:var(--primary);border:1px solid var(--border);border-radius:999px;padding:2px 10px}

/* the shell: bordered, rounded, sidebar + inset (borrowed from the fleet UI) */
.shell{display:flex;height:calc(100vh - 132px);min-height:420px;border:1px solid var(--border);
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
.row .rowlink{display:flex;align-items:center;gap:8px;flex:1;min-width:0;height:100%;
  color:inherit;text-decoration:none}
.row .pick{display:flex;align-items:center;gap:8px;padding:8px 6px 8px 0;cursor:pointer;flex:0 0 auto}
.head-right{display:flex;align-items:center;gap:8px;margin-left:auto}
.row input[type=checkbox]{flex:0 0 auto;margin:0;cursor:pointer;accent-color:var(--primary)}
.shell.collapsed .row input[type=checkbox]{display:none}
.sidebar-form{display:flex;flex-direction:column;flex:1;min-height:0}
.sidebar-foot{display:flex;gap:6px;padding:8px;border-top:1px solid var(--border);
  background:var(--bg);position:sticky;bottom:0}
.sidebar-foot .icon-btn{flex:1;border:1px solid var(--border);padding:6px 8px;font-size:12px}
.sidebar-foot .icon-btn.save{background:var(--primary);border-color:var(--primary);color:#fff}
.shell.collapsed .sidebar-foot{display:none}
.sidebar-head .all{margin-left:auto;display:flex;align-items:center;gap:6px;font-size:12px;
  cursor:pointer;user-select:none}
.shell.collapsed .sidebar-head .all{display:none}
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
_CHEVRON_ICON = (
    "<svg width=16 height=16 viewBox='0 0 24 24' fill='none' stroke='currentColor' "
    "stroke-width=2 stroke-linecap=round stroke-linejoin=round><path d='M15 6l-6 6 6 6'/></svg>"
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
        "<script>(function(){var t=localStorage.getItem('ui.theme')||'auto';"
        "var m=window.matchMedia('(prefers-color-scheme: light)');"
        "document.documentElement.dataset.theme="
        "(t==='auto'?(m.matches?'light':'dark'):t);})();</script>"
        f"<title>{html.escape(title)}</title><style>{_CSS}</style></head><body>"
        f"<div class=wrap>{body}</div></body></html>",
        status_code=status_code,
    )


def _ago(stamp: str | None) -> str:
    """'2h ago' for a human; the exact timestamp lives in the tooltip."""
    if not stamp:
        return ""
    try:
        when = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except ValueError:
        return ""
    seconds = (datetime.now(timezone.utc) - when).total_seconds()
    if seconds < 90:
        return "just now"
    minutes = seconds / 60
    if minutes < 90:
        return f"{int(minutes)}m ago"
    hours = minutes / 60
    if hours < 36:
        return f"{int(hours)}h ago"
    return f"{int(hours / 24)}d ago"


def repo_url(state) -> str:
    """Where a human can browse the served skills.

    Derived from SKILLS_SOURCE_URL so a fork points at itself:
      https://codeload.github.com/<org>/<repo>/tar.gz  ->  github.com/<org>/<repo>/tree/<ref>/<path>
    SKILLS_REPO_URL overrides it (for a mirror or a different host).
    """
    explicit = os.getenv("SKILLS_REPO_URL", "").strip()
    if explicit:
        return explicit
    url = os.getenv("SKILLS_SOURCE_URL", "").strip()
    match = re.match(r"https?://codeload\.github\.com/([^/]+)/([^/]+)/tar\.gz", url)
    if not match:
        return ""
    org, repo = match.group(1), match.group(2)
    ref = state.source.ref or "main"
    paths = (os.getenv("SKILLS_SOURCE_PATHS") or "skills").split(",")[0].strip() or "skills"
    return f"https://github.com/{org}/{repo}/tree/{ref}/{paths}"


def _source_line(state) -> str:
    """One glanceable line. The exact sha and timestamp are tooltips, not noise."""
    source = state.source
    origin = {
        "github": "GitHub",
        "local": "local checkout",
        "bundled": "bundled snapshot",
    }.get(source.origin, source.origin)
    href = repo_url(state) if source.origin == "github" else ""
    label = html.escape(origin)
    bits = [
        f"<a href='{html.escape(href)}' target=_blank rel=noopener "
        f"title='open the skills folder in the repository'>{label}</a>"
        if href
        else label
    ]
    if source.ref:
        tip = f"{source.ref} @ {source.commit}" if source.commit else source.ref
        bits.append(f"<span title='{html.escape(tip)}'>{html.escape(source.ref)}</span>")
    elif source.commit:
        bits.append(f"<span title='{html.escape(source.commit)}'>{html.escape(source.commit[:7])}</span>")
    if source.refreshed_at:
        bits.append(
            f"<span title='{html.escape(str(source.refreshed_at))}'>"
            f"updated {_ago(source.refreshed_at)}</span>"
        )
    return " · ".join(bits)


#: Last sync result per identity — rendered once, on the next page load.
_last_sync: dict[str, dict] = {}


def _flash_for(identity: str | None) -> str:
    """The sync result banner, consumed on render."""
    if not identity:
        return ""
    result = _last_sync.pop(identity, None)
    if not result:
        return ""
    if result.get("ok"):
        commit = str(result.get("commit") or "")[:7]
        text = f"Synced · {result.get('count')} skills · {result.get('ref') or 'main'} @ {commit}"
    else:
        text = f"Sync failed: {result.get('error') or 'unknown error'}"
    return f"<div class=flash>{html.escape(text)}</div>"


def _first_run_banner(identity: str | None) -> str:
    """A brand-new person has no key, so their agent cannot reach anything."""
    if not identity or prefs_mod.keys(identity):
        return ""
    return (
        "<div class=banner>You have no key yet — your agent cannot reach these skills "
        "until you mint one. <a class=pill primary href='/me'>Mint my first key</a> "
        "<a class=pill href='/how-to-connect'>How to connect</a></div>"
    )


def _shell(state, identity: str | None, selected: str | None, inset: str) -> str:
    hidden = prefs_mod.disabled(identity)
    private_names = {s.name for s in visibility_mod.private_skills(identity)}
    # hidden skills are not "visible" for tools, but the UI still lists them so
    # their owner can switch them back on.
    listed = list(visibility_mod.visible_skills(identity))
    seen = {visibility_mod.skill_id(skill, identity) for skill in listed}
    for key in sorted(hidden):
        _verdict, found = visibility_mod.resolve(key, identity, include_hidden=True)
        for skill in found:
            if visibility_mod.skill_id(skill, identity) not in seen:
                listed.append(skill)
                seen.add(visibility_mod.skill_id(skill, identity))
    listed.sort(key=lambda s: s.name)
    counts: dict[str, int] = {}
    for skill in listed:
        counts[skill.name] = counts.get(skill.name, 0) + 1

    rows = []
    for skill in listed:
        name = skill.name
        key = visibility_mod.skill_id(skill, identity)
        classes = "row"
        if key == selected:
            classes += " active"
        if key in hidden:
            classes += " off"
        # The badge describes THIS row's tier, never the name: a global and a
        # private copy share a name, and labelling both "private" is a lie.
        if skill.source_path == "private":
            badge = "<span class='badge off'>private</span>"
        else:
            files = f" ·{len(skill.files)}" if skill.files else ""
            badge = f"<span class=badge title='from git (the team copy)'>{files and 'team' + files or 'team'}</span>"
        pick = (
            f"<input type=checkbox name=visible value='{html.escape(key)}'"
            + ("" if key in hidden else " checked")
            + ">"
            if identity
            else ""
        )
        rows.append(
            f"<div class='{classes}' data-name='{html.escape(name)}'>"
            + (f"<label class=pick>{pick}{_FOLDER_ICON}</label>" if identity else _FOLDER_ICON)
            + f"<a class=rowlink href='{_href(skill, identity, counts)}'>"
            f"<span class=name>{html.escape(name)}</span>{badge}</a></div>"
        )
    if identity:
        who = f"<a class=who href='/me' title='your page: keys, private skills'>{html.escape(identity)}</a>"
        actions = (
            "<span class=head-actions>"
            "<form method=post action='/me/sync' id=syncform style='display:inline;margin:0'>"
            "<input type=hidden name=next id=syncnext value='/'>"
            "<button class=pill type=submit>Sync now</button></form>"
            "<a class='pill primary' href='/me/private/new'>＋ New Private Skill</a>"
            "<a class=pill href='/me'>My keys</a>"
            "<a class=pill href='/how-to-connect'>How to connect</a>"
            "</span>"
        )
    else:
        who = "<span class='who anon'>anonymous</span>"
        actions = ""
    theme = (
        "<span class=theme role=group aria-label='theme'>"
        "<button type=button data-theme-btn=auto aria-pressed=false>Auto</button>"
        "<button type=button data-theme-btn=light aria-pressed=false>Light</button>"
        "<button type=button data-theme-btn=dark aria-pressed=false>Dark</button></span>"
    )
    return (
        "<div class=page-head>"
        f"<h1><a href='/'>skills</a></h1><span class=count>{len(listed)} skills</span>"
        f"<span class=source>{_source_line(state)}</span>"
        f"<span class=head-right>{actions}{theme}{who}</span></div>"
        + _flash_for(identity)
        + _first_run_banner(identity)
        + "<div class=shell id=shell>"
        "<aside class=sidebar>"
        "<div class=sidebar-head>"
        "<button class=icon-btn id=toggle title='collapse or expand the sidebar'>"
        + _CHEVRON_ICON + "</button>"
        "<span class=label>Files</span>"
        + (f"<label class=all title='select or clear every skill'>"
           f"<input type=checkbox id=pickall{'' if hidden else ' checked'} "
           f"style='cursor:pointer;accent-color:var(--primary)'>"
           f"<span>All</span></label>" if identity else "")
        + "</div>"
        "<input class=search id=q placeholder='filter…' autocomplete=off>"
        + (
            "<form class=sidebar-form id=skillform method=post action='/me/skills'>"
            "<input type=hidden name=next id=next value='/'>"
            "<nav class=tree id=tree>" + "".join(rows) + "</nav>"
            "<div class=sidebar-foot>"
            "<button class=icon-btn type=submit name=bulk value=disable "
            "title='hide every skill, then Save'>Disable all</button>"
            "<button class='icon-btn save' type=submit>Save</button>"
            "</div></form>"
            if identity
            else "<nav class=tree id=tree>" + "".join(rows) + "</nav>"
        )
        + "</aside>"
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
        "const form=document.getElementById('skillform');"
        "if(form){const boxes=()=>[...form.querySelectorAll(\"input[name=visible]\")];"
        "const all=document.getElementById('pickall');"
        "const sync=()=>{const bs=boxes(),on=bs.filter(c=>c.checked).length;"
        "all.checked=on===bs.length;all.indeterminate=on>0&&on<bs.length};"
        "let dirty=false;"
        "all.addEventListener('change',()=>{boxes().forEach(c=>c.checked=all.checked);sync();dirty=true});"
        "boxes().forEach(c=>c.addEventListener('change',()=>{sync();dirty=true}));sync();"
        "document.querySelectorAll('.rowlink').forEach(a=>a.addEventListener('click',e=>{"
        "if(dirty&&!confirm('You have unsaved skill changes. Leave without saving?'))e.preventDefault()}));"
        "form.addEventListener('submit',()=>{dirty=false;"
        "document.getElementById('next').value=location.pathname});}"
        "const apply=t=>{localStorage.setItem('ui.theme',t);const m=matchMedia('(prefers-color-scheme: light)');"
        "document.documentElement.dataset.theme=(t==='auto'?(m.matches?'light':'dark'):t);"
        "document.querySelectorAll('[data-theme-btn]').forEach(x=>x.setAttribute('aria-pressed',String(x.dataset.themeBtn===t)))};"
        "document.querySelectorAll('[data-theme-btn]').forEach(b=>b.onclick=()=>apply(b.dataset.themeBtn));"
        "apply(localStorage.getItem('ui.theme')||'auto');"
        "const sf=document.getElementById('syncform');"
        "if(sf){sf.addEventListener('submit',()=>{document.getElementById('syncnext').value=location.pathname});}"
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


def _href(skill, identity: str | None, counts: dict[str, int] | None = None) -> str:
    """Names are pretty URLs; ids are unambiguous ones. Use the id only when the
    name is duplicated (team copy + yours)."""
    duplicated = (counts or {}).get(skill.name, 1) > 1
    key = visibility_mod.skill_id(skill, identity) if duplicated else skill.name
    return "/skills/" + key


def _view_toggle(name: str, raw: bool) -> str:
    if raw:
        return f"<a class=btn href='/skills/{html.escape(name)}'>rendered</a>"
    return f"<a class=btn href='/skills/{html.escape(name)}?raw=1'>raw</a>"


async def skill_detail(request):
    name = request.path_params.get("name", "")
    state = state_mod.current_state()
    identity = identity_of(request)
    verdict, found = visibility_mod.resolve(name, identity)
    if verdict == "ambiguous":
        rows = "".join(
            "<div class=panel>"
            f"<h3><a href='{_href(candidate, identity, {candidate.name: 2})}'>"
            f"{html.escape(candidate.name)}</a> "
            f"<span class=badge>{'private' if candidate.source_path == 'private' else 'team'}</span></h3>"
            f"<p>{html.escape(candidate.description)}</p></div>"
            for candidate in found
        )
        inset = (
            f"<h2>{html.escape(name)}</h2>"
            "<p class=desc>Two skills share this name — pick the one you meant.</p>"
            f"{rows}"
        )
        return _page(name, _shell(state, identity, None, inset), identity)
    skill = found[0] if found else None
    if skill is None and identity:
        # Hidden for its owner, but still theirs to switch back on.
        _verdict_hidden, hidden_found = visibility_mod.resolve(
            name, identity, include_hidden=True
        )
        skill = hidden_found[0] if hidden_found else None
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

    raw = bool(request.query_params.get("raw"))
    if raw:
        body_html = f"<h3 class=sec>Body (raw)</h3><pre class=body id=body>{html.escape(skill.body)}</pre>"
    else:
        body_html = f"<h3 class=sec>Body</h3><div class='md' id=body>{render_markdown(skill.body)}</div>"
    skill_key = visibility_mod.skill_id(skill, identity)
    hidden = identity is not None and skill_key in prefs_mod.disabled(identity)
    if identity is None:
        toggle = (
            "<div class=banner>You are browsing anonymously. "
            "Sign in (or use your token) to enable/disable skills for yourself.</div>"
        )
    else:
        label = "Enable for me" if hidden else "Disable for me"
        toggle = (
            f"<form method=post action='/skills/{html.escape(skill_key)}/toggle' style='display:inline'>"
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
        f"{_view_toggle(skill.name, raw)}"
        "<button class=btn id=copy>copy markdown</button>"
        f"{toggle}</div>"
        f"{files}"
        f"{body_html}"
        f"<textarea id=rawsrc hidden>{html.escape(skill.body)}</textarea>"
        "<script>document.getElementById('copy').onclick=async()=>{"
        "await navigator.clipboard.writeText(document.getElementById('rawsrc').value);"
        "const b=document.getElementById('copy');b.textContent='copied';"
        "setTimeout(()=>b.textContent='copy markdown',1200)};</script>"
    )
    return _page(skill.name, _shell(state, identity, skill_key, inset), identity)


async def skill_raw(request):
    name = request.path_params.get("name", "")
    _verdict, found = visibility_mod.resolve(name, identity_of(request))
    skill = found[0] if found else None
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
    _verdict, found = visibility_mod.resolve(name, identity, include_hidden=True)
    if not found:
        return _page("unknown skill", f"<h1>Unknown skill</h1><p class=desc>{html.escape(name)}</p>", identity, status_code=404)
    key = visibility_mod.skill_id(found[0], identity)
    currently_hidden = key in prefs_mod.disabled(identity)
    prefs_mod.set_disabled(identity, key, not currently_hidden)
    return RedirectResponse(f"/skills/{key}", status_code=303)


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

    hidden_ids = sorted(prefs_mod.disabled(identity))
    hidden = []
    for key in hidden_ids:
        _verdict, found = visibility_mod.resolve(key, identity, include_hidden=True)
        hidden.append(found[0].name if found else key)
    private = visibility_mod.private_skills(identity)
    keys = prefs_mod.keys(identity)
    rows = []
    for record in keys:
        active = prefs_mod.is_active(record)
        status = "active" if active else ("revoked" if record.get("revoked_at") else "expired")
        label = html.escape(record.get("label") or record.get("prefix") or record.get("id") or "")
        expires = html.escape(str(record.get("expires_at") or "never"))
        created = html.escape(str(record.get("created_at") or ""))
        action = (
            f"<form method=post action='/me/keys/{html.escape(str(record.get('id')))}/revoke' style='display:inline'>"
            "<button class=toggle type=submit>Revoke</button></form>"
            if active
            else ""
        )
        rows.append(
            "<div style='display:flex;gap:12px;align-items:center;padding:6px 0;border-top:1px solid var(--border)'>"
            f"<b style='min-width:140px'>{label}</b>"
            f"<code style='color:var(--muted)'>{html.escape(str(record.get('prefix')))}…</code>"
            f"<span style='color:var(--muted);font-size:12px'>created {created} · expires {expires} · {status}</span>"
            f"<span style='margin-left:auto'>{action}</span></div>"
        )
    key_rows = "".join(rows) or "<p>No keys yet.</p>"
    field_style = (
        "padding:6px 10px;border-radius:8px;border:1px solid var(--border);"
        "background:var(--bg);color:var(--fg)"
    )
    mint_form = (
        "<form method=post action='/me/token' style='display:flex;gap:8px;margin-top:12px;flex-wrap:wrap'>"
        f"<input name=label placeholder='label (e.g. laptop-dsh)' style='{field_style}'>"
        f"<input name=expires type=date title='optional expiry date' style='{field_style}'>"
        "<button class=toggle type=submit>Mint a key</button></form>"
    )
    token_panel = (
        f"<div class=panel><h3>Your keys ({len(keys)})</h3>"
        "<p>One key per agent. Each is shown once, and can be revoked or expire independently.</p>"
        f"{key_rows}{mint_form}</div>"
    )

    hidden_block = (
        "<div class=panel><h3>Hidden from you (" + str(len(hidden)) + ")</h3><p>"
        + (", ".join(f"<a href='/skills/{html.escape(n)}'>{html.escape(n)}</a>" for n in hidden) or "Nothing hidden.")
        + "</p><a class=btn href='/me/skills'>Manage skills</a></div>"
    )
    private_rows = "".join(
        "<div style='display:flex;gap:12px;align-items:center;padding:6px 0;border-top:1px solid var(--border)'>"
        f"<a href='/skills/{visibility_mod.skill_id(s, identity)}' style='min-width:200px'>{html.escape(s.name)}</a>"
        f"<span class=desc style='flex:1'>{html.escape(s.description[:90])}</span>"
        f"<a class=btn href='/me/private/{html.escape(s.name)}/edit'>Edit</a>"
        f"<a class=btn href='/me/private/{html.escape(s.name)}/delete'>Delete</a></div>"
        for s in private
    )
    warnings = visibility_mod.private_warnings(identity)
    warnings_block = (
        "<div class=panel><h3>Private skills that did not load ("
        + str(len(warnings))
        + ")</h3><p>These folders are on disk but the loader rejected them, so "
        "neither you nor your agent can see them.</p><ul>"
        + "".join(f"<li><code>{html.escape(w)}</code></li>" for w in warnings)
        + "</ul></div>"
        if warnings
        else ""
    )
    private_block = (
        f"<div class=panel><h3>Private skills ({len(private)})</h3>"
        + (private_rows or "<p>None yet. A private skill is visible to you alone and never lands in git.</p>")
        + "<p><a class=btn href='/me/private/new'>New private skill</a></p></div>"
    )

    body = (
        f"<h2>{html.escape(identity)}</h2>"
        f"<p class=desc>Visible to you: {len(visibility_mod.visible_skills(identity))} skills</p>"
        f"{token_panel}{hidden_block}{warnings_block}{private_block}"
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
    label = ""
    expires_at = None
    try:
        form = await request.form()
        label = str(form.get("label") or "").strip()
        raw_expiry = str(form.get("expires") or "").strip()
    except Exception:  # noqa: BLE001 - a plain POST without a body is fine
        raw_expiry = ""
    if raw_expiry:
        # <input type=date> sends YYYY-MM-DD; the key dies at the END of that day.
        expires_at = raw_expiry if len(raw_expiry) > 10 else f"{raw_expiry}T23:59:59Z"
    key = auth_mod.mint(identity, label=label, expires_at=expires_at)
    return _key_page(request, identity, key, rotated=False)


async def me_revoke(request):
    """Revoke one key from the browser."""
    from starlette.responses import RedirectResponse

    identity = identity_of(request)
    if identity is None:
        return _page("sign in required", "<h1>Sign in required</h1>", None, status_code=401)
    prefs_mod.revoke_key(identity, request.path_params.get("key_id", ""))
    return RedirectResponse("/me", status_code=303)


_INPUT_STYLE = (
    "padding:6px 10px;border-radius:8px;border:1px solid var(--border);"
    "background:var(--bg);color:var(--fg)"
)


def _signed_out(state, title: str):
    body = (
        f"<h2>{html.escape(title)}</h2>"
        "<div class=banner>This page needs an identity — sign in through the proxy, "
        "or use an API key.</div><a class=btn href='/'>back to skills</a>"
    )
    return _page(title, _shell(state, None, None, body), None, status_code=401)


async def manage_skills(request):
    """Bulk view: one checkbox per global skill, checked = visible to you."""
    from starlette.responses import HTMLResponse  # noqa: F401

    state = state_mod.current_state()
    identity = identity_of(request)
    if identity is None:
        return _signed_out(state, "manage skills")
    hidden = prefs_mod.disabled(identity)
    entries = [
        (visibility_mod.skill_id(skill, identity), skill)
        for skill in visibility_mod.all_skills(identity)
    ]
    names = entries
    rows = "".join(
        "<label style='display:flex;gap:10px;align-items:center;padding:7px 0;border-top:1px solid var(--border)'>"
        f"<input type=checkbox name=visible value='{html.escape(key)}'"
        + ("" if key in hidden else " checked")
        + f"><span>{html.escape(skill.name)}</span>"
        + f"<span class='badge off' style='margin-left:auto'>"
        f"{'private' if skill.source_path == 'private' else 'team'}</span>"
        + "</label>"
        for key, skill in entries
    )
    body = (
        f"<h2>Manage skills</h2>"
        f"<p class=desc>Unchecked skills are hidden from your agent. "
        f"{len(names) - len(hidden)} of {len(names)} visible.</p>"
        "<form method=post action='/me/skills'>"
        "<div class=panel>"
        "<label style='display:flex;gap:10px;align-items:center;padding-bottom:8px'>"
        "<input type=checkbox id=master checked><b>Select all</b></label>"
        f"{rows}</div>"
        "<div style='display:flex;gap:8px;margin-top:12px;flex-wrap:wrap'>"
        "<button class=toggle type=submit>Save</button>"
        "<button class=toggle type=button id=off>Disable all</button>"
        "<button class=toggle type=button id=on>Enable all</button>"
        "</div></form>"
        "<script>const m=document.getElementById('master');"
        "const boxes=()=>document.querySelectorAll(\"input[name=visible]\");"
        "m.addEventListener('change',()=>boxes().forEach(c=>c.checked=m.checked));"
        "document.getElementById('off').onclick=()=>boxes().forEach(c=>c.checked=false);"
        "document.getElementById('on').onclick=()=>boxes().forEach(c=>c.checked=true);</script>"
        "<p><a class=btn href='/me'>back</a></p>"
    )
    return _page("manage skills", _shell(state, identity, None, body), identity)


async def save_skills(request):
    from starlette.responses import RedirectResponse

    identity = identity_of(request)
    if identity is None:
        return _signed_out(state_mod.current_state(), "manage skills")
    form = await request.form()
    universe = {
        visibility_mod.skill_id(skill, identity)
        for skill in visibility_mod.all_skills(identity)
    }
    bulk = str(form.get("bulk") or "")
    if bulk == "disable":
        prefs_mod.set_disabled_bulk(identity, universe)          # hide everything
    elif bulk == "enable":
        prefs_mod.set_disabled_bulk(identity, set())             # show everything
    else:
        visible = {str(name) for name in form.getlist("visible")}
        prefs_mod.set_disabled_bulk(identity, universe - visible)
    next_path = str(form.get("next") or "/me/skills")
    if not next_path.startswith("/") or next_path.startswith("//"):
        next_path = "/me/skills"
    return RedirectResponse(next_path, status_code=303)


def _editor(state, identity, *, name, description, body, is_new, error="", confirm=False):
    note = (
        "<p class=desc>A private skill is visible only to you. The name must be unique — "
        "it cannot reuse a global skill's name.</p>"
    )
    error_html = f"<div class=banner style='border-color:#c0392b'>{html.escape(error)}</div>" if error else ""
    name_field = (
        f"<input name=name value='{html.escape(name)}' placeholder='my-runbook' "
        f"autocomplete=off spellcheck=false style='{_INPUT_STYLE};width:100%'>"
        if is_new
        else f"<input name=name value='{html.escape(name)}' readonly style='{_INPUT_STYLE};width:100%;opacity:.7'>"
    )
    action = "/me/private" if is_new else f"/me/private/{html.escape(name)}"
    confirm_field = "<input type=hidden name=confirm value=1>" if confirm else ""
    body_html = (
        f"<h2>{'New private skill' if is_new else 'Edit ' + html.escape(name)}</h2>"
        f"{error_html}{note}"
        f"<form method=post action='{action}' class=panel autocomplete=off>"
        f"{confirm_field}"
        "<p><b>Name</b><br>"
        f"{name_field}</p>"
        "<p><b>Description</b> — one line: when should an agent use this?<br>"
        f"<input name=description value='{html.escape(description)}' autocomplete=off "
        f"style='{_INPUT_STYLE};width:100%'></p>"
        "<p><b>Body</b> — markdown<br>"
        "<textarea name=body rows=18 "
        f"style='{_INPUT_STYLE};width:100%;font-family:ui-monospace,monospace'>{html.escape(body)}</textarea></p>"
        "<button class=toggle type=submit>Save</button> "
        "<a class=btn href='/me'>cancel</a>"
        "</form>"
    )
    return _page("private skill", _shell(state, identity, None, body_html), identity)


async def private_new(request):
    state = state_mod.current_state()
    identity = identity_of(request)
    if identity is None:
        return _signed_out(state, "new private skill")
    return _editor(state, identity, name="", description="", body="", is_new=True)


async def private_edit(request):
    state = state_mod.current_state()
    identity = identity_of(request)
    if identity is None:
        return _signed_out(state, "edit private skill")
    name = request.path_params.get("name", "")
    current = private_mod.read(identity, name)
    if current is None:
        return _page("not found", _shell(state, identity, None,
                     f"<h2>No private skill {html.escape(name)!r}</h2><a class=btn href='/me'>back</a>"),
                     identity, status_code=404)
    return _editor(state, identity, name=current["name"], description=current["description"],
                   body=current["body"], is_new=False)


async def private_create(request):
    return await _private_write(request, name_from_form=True)


async def private_update(request):
    return await _private_write(request, name_from_form=False)


async def _private_write(request, *, name_from_form: bool):
    from starlette.responses import RedirectResponse

    state = state_mod.current_state()
    identity = identity_of(request)
    if identity is None:
        return _signed_out(state, "private skill")
    form = await request.form()
    name = str(form.get("name") or "").strip() if name_from_form else request.path_params.get("name", "")
    description = str(form.get("description") or "")
    body = str(form.get("body") or "")
    # Duplicate names are allowed ACROSS tiers (a private copy sits next to the
    # team's). Within your own tier the name IS the folder, so a create with an
    # existing name would silently replace that skill — ask first.
    if name_from_form:
        wanted = (name or "").strip().lower()
        if private_mod.read(identity, wanted) is not None and not str(form.get("confirm") or ""):
            return _editor(
                state,
                identity,
                name=wanted,
                description=description,
                body=body,
                is_new=True,
                error=(
                    f"You already have a private skill called {wanted!r}. "
                    "Saving again will REPLACE it — change the name to create a new "
                    "one, or press Save to overwrite."
                ),
                confirm=True,
            )
    try:
        saved = private_mod.save(identity, name, description, body, forbidden=set())
    except private_mod.PrivateError as exc:
        return _editor(state, identity, name=name, description=description, body=body,
                       is_new=name_from_form, error=str(exc))
    return RedirectResponse(
        f"/skills/private:{identity}:{saved}", status_code=303
    )


async def private_delete_page(request):
    state = state_mod.current_state()
    identity = identity_of(request)
    if identity is None:
        return _signed_out(state, "delete private skill")
    name = html.escape(request.path_params.get("name", ""))
    body = (
        f"<h2>Delete {name}?</h2>"
        "<p class=desc>This removes your private copy. It cannot be undone.</p>"
        f"<form method=post action='/me/private/{name}/delete'>"
        "<button class=toggle type=submit>Delete</button></form>"
        "<p><a class=btn href='/me'>cancel</a></p>"
    )
    return _page("delete", _shell(state, identity, None, body), identity)


async def private_delete(request):
    from starlette.responses import RedirectResponse

    identity = identity_of(request)
    if identity is None:
        return _signed_out(state_mod.current_state(), "delete private skill")
    private_mod.delete(identity, request.path_params.get("name", ""))
    return RedirectResponse("/me", status_code=303)


async def me_sync(request):
    """Fetch the latest main and swap it in. Any signed-in person may do this:
    the swap is atomic and idempotent, and it is what makes a git merge visible."""
    from starlette.responses import RedirectResponse

    identity = identity_of(request)
    if identity is None:
        return _signed_out(state_mod.current_state(), "sync")
    form = await request.form()
    next_path = str(form.get("next") or "/")
    if not next_path.startswith("/") or next_path.startswith("//"):
        next_path = "/"
    try:
        payload = await asyncio.to_thread(refresh_mod.refresh_from_source)
        _last_sync[identity] = {
            "ok": True,
            "commit": payload.get("commit"),
            "count": payload.get("count"),
            "ref": payload.get("ref"),
        }
    except Exception as exc:  # noqa: BLE001 - the previous release keeps serving
        _last_sync[identity] = {"ok": False, "error": str(exc)[:200]}
    return RedirectResponse(next_path, status_code=303)


def _how_to_connect(state, identity: str | None, host: str) -> str:
    base = f"https://{host}" if host else "https://skills-mcp.<domain>"
    step = "display:flex;gap:14px;padding:12px 0;border-top:1px solid var(--border);align-items:flex-start"
    num = (
        "flex:0 0 24px;height:24px;border-radius:50%;background:var(--primary);color:#fff;"
        "display:flex;align-items:center;justify-content:center;font-size:12px;font-weight:600"
    )
    code = (
        "background:var(--surface-2);padding:2px 6px;border-radius:4px;font-size:12px"
    )
    body = (
        "<h2>How to connect</h2>"
        "<p class=desc>Mint a key, then use it — through the gateway, directly, or both.</p>"
        "<div class=panel>"
        f"<div style='{step}'><div style='{num}'>1</div><div>"
        "<b>Mint a key</b><br><span class=desc>One key per agent. It is shown once.</span><br>"
        "<a class='pill primary' href='/me'>Mint a key</a></div></div>"
        f"<div style='{step}'><div style='{num}'>2</div><div>"
        "<b>Copy it</b><br><span class=desc>It looks like "
        f"<code style='{code}'>sk_live_…</code>. Lost it? Revoke it and mint another.</span></div></div>"
        f"<div style='{step}'><div style='{num}'>3</div><div style='flex:1'>"
        "<b>Use it — either or both</b>"
        f"<p class=desc style='margin:10px 0 4px'><b>(a) Through the PCAI gateway</b> — "
        "the governed path, recommended for agents.</p>"
        "<span class=desc>Gateway console → Keys → your key → creds → <code style='"
        f"{code}'>skills-mcp</code> → header <code style='{code}'>Authorization</code>, "
        f"value <code style='{code}'>Bearer sk_live_…</code> → Save.</span>"
        f"<p class=desc style='margin:14px 0 4px'><b>(b) Directly</b> — for Langflow, the MCP "
        "Inspector, or any client that can set a header. No SSO, no gateway.</p>"
        f"<span class=desc>URL <code style='{code}'>{html.escape(base)}/mcp</code>, "
        f"header <code style='{code}'>Authorization: Bearer sk_live_…</code></span>"
        "</div></div>"
        f"<div style='{step}'><div style='{num}'>4</div><div>"
        "<b>What you get</b><br><span class=desc>Three tools — "
        f"<code style='{code}'>list_skills</code>, <code style='{code}'>get_skill</code>, "
        f"<code style='{code}'>get_skill_file</code> — filtered to your own view: the skills you "
        "enabled, plus your private ones.</span></div></div>"
        "</div>"
        "<p><a class=btn href='/'>back to skills</a></p>"
    )
    return body


async def how_to_connect(request):
    state = state_mod.current_state()
    identity = identity_of(request)
    host = request.headers.get("host", "")
    body = _how_to_connect(state, identity, host)
    return _page("how to connect", _shell(state, identity, None, body), identity)


def build_ui_routes() -> list:
    """The browser routes, or [] when SKILLS_UI_ENABLED=0."""
    if not ui_enabled():
        return []
    return [
        Route("/", index),
        Route("/me", me_page),
        Route("/how-to-connect", how_to_connect),
        Route("/me/sync", me_sync, methods=["POST"]),
        Route("/me/skills", manage_skills),
        Route("/me/skills", save_skills, methods=["POST"]),
        Route("/me/private/new", private_new),
        Route("/me/private", private_create, methods=["POST"]),
        Route("/me/private/{name}/edit", private_edit),
        Route("/me/private/{name}/delete", private_delete_page),
        Route("/me/private/{name}/delete", private_delete, methods=["POST"]),
        Route("/me/private/{name}", private_update, methods=["POST"]),
        Route("/me/token", me_token, methods=["POST"]),
        Route("/me/keys/{key_id}/revoke", me_revoke, methods=["POST"]),
        Route("/skills/{name}", skill_detail),
        Route("/skills/{name}/toggle", skill_toggle, methods=["POST"]),
        Route("/skills/{name}/raw", skill_raw),
    ]
