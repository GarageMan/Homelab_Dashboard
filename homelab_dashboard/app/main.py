"""
Homelab Dashboard – Aggregator
Sammelt Daten von HASS-Pi (Supervisor/Core), Ubuntu-Server & Pi-hole (Glances),
Pi-hole (v6-API) und dem Claude-Usage-Exporter und serviert sie als JSON.
Jede Quelle ist gekapselt: faellt eine aus, zeigt die Kachel "n/a",
das Board bleibt stehen.
"""
import asyncio
import json
import os
import time
from pathlib import Path

import httpx
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

# ------------------------------------------------------------------ Konfig ----
OPTIONS_FILE = "/data/options.json"


def load_options() -> dict:
    """Optionen aus dem Add-on lesen; fuer lokale Tests via ENV ueberschreibbar."""
    opts = {}
    try:
        opts = json.loads(Path(OPTIONS_FILE).read_text())
    except Exception:
        pass
    return {
        "ubuntu_host": os.getenv("UBUNTU_HOST", opts.get("ubuntu_host", "192.168.1.75")),
        "pihole_host": os.getenv("PIHOLE_HOST", opts.get("pihole_host", "192.168.1.5")),
        "glances_port": int(os.getenv("GLANCES_PORT", opts.get("glances_port", 61208))),
        "pihole_password": os.getenv("PIHOLE_PASSWORD", opts.get("pihole_password", "")),
        "usage_url": os.getenv("USAGE_URL", opts.get("usage_url", "")),
        "refresh_seconds": int(os.getenv("REFRESH_SECONDS", opts.get("refresh_seconds", 15))),
    }


OPT = load_options()
SUPERVISOR_TOKEN = os.getenv("SUPERVISOR_TOKEN", "")
SUP = "http://supervisor"
MOCK = os.getenv("HOMELAB_MOCK") == "1"

# Systemmonitor-Entities des HASS-Pi (bei Bedarf hier anpassen)
HA_SENSORS = {
    "cpu":  "sensor.system_monitor_prozessornutzung",
    "temp": "sensor.system_monitor_prozessortemperatur",
    "mem":  "sensor.system_monitor_arbeitsspeicherauslastung",
    "disk": "sensor.disk_use_percent",
}

app = FastAPI(title="Homelab Dashboard")
STATIC = Path(__file__).parent / "static"


# --------------------------------------------------------------- Helfer -------
def _fmt_uptime(seconds: float) -> str:
    seconds = int(seconds)
    d, r = divmod(seconds, 86400)
    h, r = divmod(r, 3600)
    m, _ = divmod(r, 60)
    parts = []
    if d:
        parts.append(f"{d} T")
    if h or d:
        parts.append(f"{h} Std")
    parts.append(f"{m} Min")
    return " ".join(parts)


def _num(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


# ------------------------------------------------------- Collector: Glances ---
async def collect_glances(client: httpx.AsyncClient, host: str, port: int, label: str) -> dict:
    """Ein einziger /api/4/all-Call liefert CPU, RAM, FS, Sensoren, Netz, Uptime, System."""
    base = f"http://{host}:{port}/api/4"
    try:
        r = await client.get(f"{base}/all", timeout=4.0)
        r.raise_for_status()
        d = r.json()
    except Exception as e:
        return {"ok": False, "label": label, "host": host, "error": str(e)}

    system = d.get("system", {}) or {}
    mem = d.get("mem", {}) or {}
    cpu = d.get("cpu", {}) or {}
    load = d.get("load", {}) or {}

    # Root-Dateisystem
    root = {}
    for fs in d.get("fs", []) or []:
        if fs.get("mnt_point") == "/":
            root = fs
            break
    if not root and d.get("fs"):
        root = d["fs"][0]

    # CPU-Temperatur aus den Sensoren fischen
    temp = None
    for s in d.get("sensors", []) or []:
        lbl = str(s.get("label", "")).lower()
        typ = str(s.get("type", "")).lower()
        if "temp" in typ and any(k in lbl for k in ("cpu", "core", "package", "thermal", "tctl", "soc")):
            temp = _num(s.get("value"))
            break
    if temp is None:
        for s in d.get("sensors", []) or []:
            if "temp" in str(s.get("type", "")).lower():
                temp = _num(s.get("value"))
                break

    # Netzdurchsatz (Summe ueber echte Interfaces)
    rx = tx = 0.0
    for n in d.get("network", []) or []:
        name = n.get("interface_name") or n.get("name") or ""
        if name in ("lo", "") or name.startswith(("docker", "veth", "br-")):
            continue
        rx += _num(n.get("bytes_recv_rate_per_sec", n.get("bytes_recv_gauge", 0)))
        tx += _num(n.get("bytes_sent_rate_per_sec", n.get("bytes_sent_gauge", 0)))

    up = d.get("uptime")
    up_str = up if isinstance(up, str) else _fmt_uptime(_num(up))

    osname = system.get("linux_distro") or system.get("os_name") or system.get("hr_name") or "?"

    return {
        "ok": True,
        "label": label,
        "host": host,
        "hostname": system.get("hostname", host),
        "os": osname,
        "uptime": up_str,
        "cpu_pct": round(_num(cpu.get("total")), 1),
        "temp_c": round(temp, 1) if temp is not None else None,
        "mem_pct": round(_num(mem.get("percent")), 1),
        "mem_used": _num(mem.get("used")),
        "mem_total": _num(mem.get("total")),
        "disk_pct": round(_num(root.get("percent")), 1),
        "disk_used": _num(root.get("used")),
        "disk_total": _num(root.get("size")),
        "load": [round(_num(load.get("min1")), 2), round(_num(load.get("min5")), 2),
                 round(_num(load.get("min15")), 2)],
        "net_rx": rx,
        "net_tx": tx,
    }


# ------------------------------------------------------- Collector: Pi-hole ---
async def collect_pihole(client: httpx.AsyncClient, host: str, password: str) -> dict:
    base = f"http://{host}/api"
    sid = None
    try:
        if password:
            a = await client.post(f"{base}/auth", json={"password": password}, timeout=4.0)
            a.raise_for_status()
            sid = (a.json().get("session") or {}).get("sid")
            if not sid:
                return {"ok": False, "host": host, "error": "auth fehlgeschlagen"}
        headers = {"X-FTL-SID": sid} if sid else {}

        s = await client.get(f"{base}/stats/summary", headers=headers, timeout=4.0)
        s.raise_for_status()
        summary = s.json()

        b = await client.get(f"{base}/dns/blocking", headers=headers, timeout=4.0)
        blocking = b.json().get("blocking", "unknown") if b.status_code == 200 else "unknown"

        q = summary.get("queries", {}) or {}
        grav = summary.get("gravity", {}) or {}
        clients = summary.get("clients", {}) or {}

        out = {
            "ok": True,
            "host": host,
            "blocking": blocking,
            "queries": int(_num(q.get("total"))),
            "blocked": int(_num(q.get("blocked"))),
            "percent": round(_num(q.get("percent_blocked")), 1),
            "gravity": int(_num(grav.get("domains_being_blocked"))),
            "clients_active": int(_num(clients.get("active"))),
        }

        # PADD-artige Extras (optional, best effort)
        try:
            td = await client.get(f"{base}/stats/top_domains",
                                  params={"blocked": "true", "count": 1}, headers=headers, timeout=4.0)
            arr = td.json().get("domains") or td.json().get("top_domains") or []
            if arr:
                out["top_blocked"] = arr[0].get("domain")
        except Exception:
            pass
        try:
            tc = await client.get(f"{base}/stats/top_clients",
                                  params={"count": 1}, headers=headers, timeout=4.0)
            arr = tc.json().get("clients") or tc.json().get("top_clients") or []
            if arr:
                out["top_client"] = arr[0].get("name") or arr[0].get("ip")
        except Exception:
            pass

        return out
    except Exception as e:
        return {"ok": False, "host": host, "error": str(e)}
    finally:
        if sid:
            try:
                await client.request("DELETE", f"{base}/auth", headers={"X-FTL-SID": sid}, timeout=3.0)
            except Exception:
                pass


# ---------------------------------------------- Collector: HASS (Supervisor) --
async def _sup_get(client, path):
    r = await client.get(f"{SUP}{path}",
                         headers={"Authorization": f"Bearer {SUPERVISOR_TOKEN}"}, timeout=4.0)
    r.raise_for_status()
    return r.json().get("data", r.json())


async def _core_state(client, entity):
    try:
        r = await client.get(f"{SUP}/core/api/states/{entity}",
                             headers={"Authorization": f"Bearer {SUPERVISOR_TOKEN}"}, timeout=4.0)
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return None


async def collect_hass(client: httpx.AsyncClient) -> dict:
    if not SUPERVISOR_TOKEN:
        return {"ok": False, "error": "kein SUPERVISOR_TOKEN"}
    try:
        sup, core, osinfo, host = await asyncio.gather(
            _sup_get(client, "/supervisor/info"),
            _sup_get(client, "/core/info"),
            _sup_get(client, "/os/info"),
            _sup_get(client, "/host/info"),
            return_exceptions=True,
        )
        sup = sup if isinstance(sup, dict) else {}
        core = core if isinstance(core, dict) else {}
        osinfo = osinfo if isinstance(osinfo, dict) else {}
        host = host if isinstance(host, dict) else {}

        addon_updates = sum(1 for a in sup.get("addons", []) if a.get("update_available"))
        updates = {
            "core": bool(core.get("update_available")),
            "os": bool(osinfo.get("update_available")),
            "supervisor": bool(sup.get("update_available")),
            "addons": addon_updates,
        }
        updates_total = sum(1 for k in ("core", "os", "supervisor") if updates[k]) + addon_updates

        # Boot-Zeit -> Uptime (Supervisor liefert boot_timestamp in Mikrosekunden)
        uptime = None
        bt = host.get("boot_timestamp")
        if bt:
            uptime = _fmt_uptime(time.time() - _num(bt) / 1_000_000)

        # Live-Metriken via Systemmonitor (falls vorhanden)
        live = {}
        states = await asyncio.gather(*[_core_state(client, e) for e in HA_SENSORS.values()])
        for key, st in zip(HA_SENSORS.keys(), states):
            if st and st.get("state") not in (None, "unknown", "unavailable"):
                live[key] = _num(st["state"])

        # Entitaets-Gesundheit
        ent = {"total": None, "automations": None, "unavailable": None}
        try:
            r = await client.get(f"{SUP}/core/api/states",
                                 headers={"Authorization": f"Bearer {SUPERVISOR_TOKEN}"}, timeout=6.0)
            if r.status_code == 200:
                allst = r.json()
                ent["total"] = len(allst)
                ent["automations"] = sum(1 for s in allst if s["entity_id"].startswith("automation."))
                ent["unavailable"] = sum(1 for s in allst if s.get("state") in ("unavailable", "unknown"))
        except Exception:
            pass

        return {
            "ok": True,
            "hostname": host.get("hostname", "homeassistant"),
            "os": osinfo.get("board") and f"HAOS {osinfo.get('version')}" or host.get("operating_system", "HAOS"),
            "kernel": host.get("kernel"),
            "ha_version": core.get("version"),
            "uptime": uptime,
            "updates": updates,
            "updates_total": updates_total,
            "cpu_pct": live.get("cpu"),
            "temp_c": live.get("temp"),
            "mem_pct": live.get("mem"),
            "disk_pct": live.get("disk") if "disk" in live else (
                round(_num(host.get("disk_used")) / _num(host.get("disk_total"), 1) * 100, 1)
                if host.get("disk_total") else None),
            "disk_used": _num(host.get("disk_used")),
            "disk_total": _num(host.get("disk_total")),
            "entities": ent,
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ------------------------------------------------ Collector: Claude-Usage -----
async def collect_usage(client: httpx.AsyncClient, url: str) -> dict:
    if not url:
        return {"ok": False, "error": "keine usage_url gesetzt"}
    try:
        r = await client.get(url, timeout=5.0)
        r.raise_for_status()
        return {"ok": True, **r.json()}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ------------------------------------------------------------ Mock ------------
def _mock():
    now = time.time()
    return {
        "hass": {"ok": True, "hostname": "homeassistant", "os": "HAOS 18.1", "kernel": "6.6.31",
                 "ha_version": "2026.6.4", "uptime": "4 T 3 Std 12 Min",
                 "updates": {"core": True, "os": False, "supervisor": False, "addons": 2},
                 "updates_total": 3, "cpu_pct": 14.2, "temp_c": 47.2, "mem_pct": 41.0,
                 "disk_pct": 38.0, "disk_used": 12e9, "disk_total": 31e9,
                 "entities": {"total": 412, "automations": 37, "unavailable": 3}},
        "ubuntu": {"ok": True, "label": "Ubuntu-Server", "host": "192.168.1.75",
                   "hostname": "ubuntu-srv", "os": "Ubuntu 24.04.4 LTS", "uptime": "21 T 6 Std 4 Min",
                   "cpu_pct": 22.5, "temp_c": 51.0, "mem_pct": 63.0, "mem_used": 10.1e9,
                   "mem_total": 16e9, "disk_pct": 71.0, "disk_used": 1.4e12, "disk_total": 2e12,
                   "load": [1.45, 1.23, 0.98], "net_rx": 5.8e6, "net_tx": 1.2e6},
        "pihole": {"ok": True, "host": "192.168.1.5", "blocking": "enabled", "queries": 93157,
                   "blocked": 18342, "percent": 19.7, "gravity": 151284, "clients_active": 12,
                   "top_blocked": "graph.facebook.com", "top_client": "192.168.1.31"},
        "usage": {"ok": True, "session_pct": 61, "session_reset": now + 13440,
                  "weekly_pct": 11, "weekly_reset": now + 266400, "plan": "Max"},
        "ts": now,
    }


# ------------------------------------------------------------ Routen ----------
@app.get("/api/data")
async def api_data():
    if MOCK:
        return JSONResponse(_mock())
    async with httpx.AsyncClient() as client:
        hass, ubuntu, pihole, usage = await asyncio.gather(
            collect_hass(client),
            collect_glances(client, OPT["ubuntu_host"], OPT["glances_port"], "Ubuntu-Server"),
            collect_pihole(client, OPT["pihole_host"], OPT["pihole_password"]),
            collect_usage(client, OPT["usage_url"]),
        )
    return JSONResponse({"hass": hass, "ubuntu": ubuntu, "pihole": pihole,
                         "usage": usage, "ts": time.time()})


@app.get("/api/config")
async def api_config():
    return {"refresh_seconds": OPT["refresh_seconds"]}


@app.get("/", response_class=HTMLResponse)
@app.get("/index.html", response_class=HTMLResponse)
async def index():
    return HTMLResponse((STATIC / "index.html").read_text(encoding="utf-8"))
