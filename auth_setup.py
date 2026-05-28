"""
Веб-эндпоинт /auth для добавления Zo-аккаунтов из docker-контейнера
(где Playwright/Patchright нет, и host-flow недоступен).

UX:
  1. Юзер запускает ZoAPI в Docker.
  2. Открывает в браузере http://localhost:17878/auth
  3. Получает HTML-страницу с инструкцией:
       - в соседней вкладке открыть https://<твой-домен>.zo.computer
       - залогиниться
       - открыть DevTools → Application → Cookies
       - скопировать access_token и refresh_token
       - вернуться к /auth и вставить значения в форму
  4. POST /auth/save с {label, access_token, refresh_token}
       - прокси валидирует токены через `/personas/available`
       - сохраняет аккаунт в STORE
       - возвращает успех или ошибку

Никакого Playwright не нужно — пейстинг руками работает в любой среде.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

if TYPE_CHECKING:
    from accounts import Account, AccountStore
    from zo_client import ZoClient

log = logging.getLogger("zo-proxy.auth")


_AUTH_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>ZoAPI · add account</title>
<style>
  :root { color-scheme: dark; }
  body {
    background: #0c0c0e; color: #e8e6e3;
    font-family: ui-monospace, "JetBrains Mono", Menlo, Consolas, monospace;
    max-width: 760px; margin: 40px auto; padding: 0 24px;
    line-height: 1.55;
  }
  h1 { font-size: 1.4rem; margin: 0 0 8px; color: #d8a657; }
  h2 { font-size: 1.05rem; margin: 28px 0 8px; color: #aaa39a; text-transform: uppercase; letter-spacing: .08em; }
  ol { padding-left: 20px; }
  ol li { margin: 8px 0; }
  code { background: #1a1a1f; padding: 1px 6px; border-radius: 4px; color: #f5d77a; }
  input, textarea, button {
    font: inherit; background: #1a1a1f; color: #e8e6e3;
    border: 1px solid #333; border-radius: 6px; padding: 10px 12px;
    width: 100%; box-sizing: border-box;
  }
  textarea { min-height: 64px; resize: vertical; font-family: ui-monospace, Menlo, monospace; }
  label { display: block; margin: 14px 0 4px; color: #aaa39a; font-size: .85rem; text-transform: uppercase; letter-spacing: .06em; }
  button { background: #d8a657; color: #0c0c0e; border-color: #d8a657; font-weight: 600; margin-top: 18px; cursor: pointer; }
  button:hover { background: #e6b167; }
  .ok { color: #8ec07c; }
  .err { color: #fb4934; }
  pre { background: #1a1a1f; padding: 12px; border-radius: 6px; white-space: pre-wrap; word-break: break-all; }
  small { color: #777; }
</style>
</head>
<body>
<h1>ZoAPI · add a Zo account</h1>
<p>
  Use this page when you run ZoAPI inside Docker (or anywhere there is no
  GUI browser). You'll paste two cookies from your normal browser session
  on <code>zo.computer</code>.
</p>

<h2>How to get the cookies</h2>
<ol>
  <li>Open <code>https://&lt;your-handle&gt;.zo.computer</code> in any browser and sign in.</li>
  <li>Open DevTools (<code>F12</code> or <code>Cmd+Opt+I</code>) → <strong>Application</strong> → <strong>Cookies</strong> → pick the <code>zo.computer</code> origin.</li>
  <li>Copy the value of <code>access_token</code> and <code>refresh_token</code> into the form below.</li>
  <li>Pick a label (any short word, e.g. <code>main</code>, <code>alt1</code>) — used only locally to switch between accounts.</li>
</ol>

<form id="f">
  <label>Label</label>
  <input id="label" placeholder="main" autocomplete="off">

  <label>access_token</label>
  <textarea id="access" placeholder="eyJhbGciOi…" autocomplete="off"></textarea>

  <label>refresh_token</label>
  <textarea id="refresh" placeholder="eyJhbGciOi…" autocomplete="off"></textarea>

  <button type="submit">Validate &amp; save</button>
</form>

<p id="status"></p>
<pre id="result" hidden></pre>

<small>
  Tokens are kept only in <code>accounts.json</code> on your local disk
  (or the volume you mounted into Docker). ZoAPI never sends them
  anywhere except <code>api.zo.computer</code> on your behalf.
</small>

<script>
document.getElementById("f").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const status = document.getElementById("status");
  const result = document.getElementById("result");
  status.textContent = "checking…";
  status.className = "";
  result.hidden = true;

  const body = {
    label:         document.getElementById("label").value.trim() || "main",
    access_token:  document.getElementById("access").value.trim(),
    refresh_token: document.getElementById("refresh").value.trim(),
  };
  if (!body.access_token || !body.refresh_token) {
    status.textContent = "fill in both tokens";
    status.className = "err";
    return;
  }

  try {
    const r = await fetch("/auth/save", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
    });
    const j = await r.json();
    if (r.ok && j.ok) {
      status.textContent = "✓ saved — account " + j.label + " is now usable";
      status.className = "ok";
      result.textContent = JSON.stringify(j, null, 2);
      result.hidden = false;
    } else {
      status.textContent = "✗ " + (j.error || ("HTTP " + r.status));
      status.className = "err";
    }
  } catch (e) {
    status.textContent = "✗ " + e.message;
    status.className = "err";
  }
});
</script>
</body>
</html>
"""


def install(app, STORE: "AccountStore", ZO: "ZoClient") -> None:
    """Регистрирует /auth и /auth/save на FastAPI-приложении."""
    from accounts import Account, extract_domain_from_access_token, clean_domain

    @app.get("/auth", include_in_schema=False)
    async def _auth_page() -> HTMLResponse:
        return HTMLResponse(_AUTH_PAGE)

    @app.post("/auth/save")
    async def _auth_save(req: Request) -> JSONResponse:
        try:
            body = await req.json()
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"bad json: {e}")
        label = (body.get("label") or "").strip() or "main"
        access = (body.get("access_token") or "").strip()
        refresh = (body.get("refresh_token") or "").strip()
        if not access or not refresh:
            return JSONResponse({"ok": False, "error": "both tokens required"}, status_code=400)

        domain = (
            (body.get("domain") or "").strip()
            or extract_domain_from_access_token(access)
            or ""
        )
        domain = clean_domain(domain) or "user"

        acc = Account(
            label=label,
            domain=domain,
            access_token=access,
            refresh_token=refresh,
            added_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        )
        # Validate by hitting /personas/available
        try:
            await ZO.list_personas(acc)
        except Exception as e:
            return JSONResponse(
                {"ok": False, "error": f"invalid tokens: {e}"}, status_code=400
            )

        # Replace existing same-label or add fresh
        STORE.remove(label)
        STORE.add(acc, make_active=(len(STORE.accounts) == 0))
        return JSONResponse(
            {
                "ok": True,
                "label": acc.label,
                "domain": acc.domain,
                "active": STORE.active_label,
                "total_accounts": len(STORE.accounts),
            }
        )
