#!/usr/bin/env python3
"""
Claude-Usage-Exporter
=====================
Laeuft auf dem Ubuntu-Server (immer an). Liest das Claude-Code-OAuth-Token aus
~/.claude/.credentials.json, macht einen minimalen API-Call (max_tokens=1) und
liest die kontoweiten Rate-Limit-Header aus der Antwort:

    anthropic-ratelimit-unified-5h-utilization / -reset   -> Session (5 h)
    anthropic-ratelimit-unified-7d-utilization / -reset   -> Weekly  (7 d)

Diese Zahlen entsprechen der claude.ai-Usage-Seite. Ergebnis wird unter
http://<host>:8787/usage als JSON bereitgestellt und alle CACHE_TTL Sekunden
aufgefrischt (nicht bei jedem Dashboard-Poll -> schont das Konto).

Nur Python-Standardbibliothek. Start via systemd (siehe README).

Hinweis: Der API-Aufruf mit OAuth-Token ist eine inoffizielle, aber von
mehreren Open-Source-Tools genutzte Methode. Bei einem 401 ist das Token
abgelaufen -> einmal `claude` auf dem Server ausfuehren (oder den Keep-alive-
Timer aktivieren), dann erneuert Claude Code die Credentials-Datei.
"""
import json
import os
import time
import threading
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# --------------------------------------------------------------- Konfig -------
PORT = int(os.getenv("USAGE_PORT", "8787"))
BIND = os.getenv("USAGE_BIND", "0.0.0.0")
CACHE_TTL = int(os.getenv("USAGE_TTL", "90"))            # Sekunden
CRED = os.path.expanduser(os.getenv("CLAUDE_CRED", "~/.claude/.credentials.json"))
MODEL = os.getenv("USAGE_MODEL", "claude-haiku-4-5-20251001")
API_URL = "https://api.anthropic.com/v1/messages"

_cache = {"ts": 0, "data": {"ok": False, "error": "noch kein Abruf"}}
_lock = threading.Lock()


def read_token() -> str | None:
    """Access-Token aus der Claude-Code-Credentials-Datei ziehen (defensiv)."""
    try:
        raw = json.loads(open(CRED, encoding="utf-8").read())
    except Exception:
        return None
    # Bekannte Struktur: {"claudeAiOauth": {"accessToken": "..."}}
    for path in (("claudeAiOauth", "accessToken"), ("accessToken",), ("access_token",)):
        cur = raw
        ok = True
        for key in path:
            if isinstance(cur, dict) and key in cur:
                cur = cur[key]
            else:
                ok = False
                break
        if ok and isinstance(cur, str):
            return cur
    return None


def fetch_usage() -> dict:
    token = read_token()
    if not token:
        return {"ok": False, "error": f"kein Token in {CRED} (Claude Code angemeldet?)"}

    body = json.dumps({
        "model": MODEL,
        "max_tokens": 1,
        "messages": [{"role": "user", "content": "."}],
    }).encode()

    req = urllib.request.Request(API_URL, data=body, method="POST")
    req.add_header("content-type", "application/json")
    req.add_header("authorization", f"Bearer {token}")
    req.add_header("anthropic-version", "2023-06-01")
    # OAuth-Zugriff auf die Messages-API benoetigt diesen Beta-Header.
    # Falls Anthropic den Namen aendert, hier anpassen.
    req.add_header("anthropic-beta", "oauth-2025-04-20")

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            hdr = {k.lower(): v for k, v in resp.headers.items()}
    except urllib.error.HTTPError as e:
        hdr = {k.lower(): v for k, v in e.headers.items()} if e.headers else {}
        # Rate-Limit-Header liegen auch bei manchen Fehlercodes vor:
        if "anthropic-ratelimit-unified-5h-utilization" not in hdr:
            snippet = ""
            try:
                snippet = e.read().decode()[:200]
            except Exception:
                pass
            return {"ok": False, "error": f"HTTP {e.code}: {snippet}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}

    def pct(key):
        v = hdr.get(f"anthropic-ratelimit-unified-{key}-utilization")
        return round(float(v) * 100, 1) if v is not None else None

    def reset(key):
        v = hdr.get(f"anthropic-ratelimit-unified-{key}-reset")
        return int(v) if v is not None else None

    session_pct = pct("5h")
    if session_pct is None:
        return {"ok": False, "error": "keine unified-ratelimit-Header in der Antwort"}

    return {
        "ok": True,
        "session_pct": session_pct,
        "session_reset": reset("5h"),
        "weekly_pct": pct("7d"),
        "weekly_reset": reset("7d"),
        "plan": hdr.get("anthropic-ratelimit-unified-fallback-plan", ""),
        "fetched": int(time.time()),
    }


def cached() -> dict:
    with _lock:
        if time.time() - _cache["ts"] < CACHE_TTL and _cache["data"].get("ok"):
            return _cache["data"]
        data = fetch_usage()
        if data.get("ok"):
            _cache["data"] = data
            _cache["ts"] = time.time()
        elif not _cache["data"].get("ok"):
            _cache["data"] = data          # Fehler zeigen, wenn nie erfolgreich
        return _cache["data"] if _cache["data"].get("ok") else data


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.rstrip("/") in ("/usage", ""):
            payload = json.dumps(cached()).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("access-control-allow-origin", "*")
            self.send_header("content-length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        else:
            self.send_error(404)

    def log_message(self, *a):
        pass  # still


if __name__ == "__main__":
    print(f"Claude-Usage-Exporter auf http://{BIND}:{PORT}/usage (TTL {CACHE_TTL}s, Modell {MODEL})")
    ThreadingHTTPServer((BIND, PORT), Handler).serve_forever()
