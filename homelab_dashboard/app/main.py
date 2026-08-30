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
        "website_url": os.getenv("WEBSITE_URL", opts.get("website_url", "")),
        "website_name": os.getenv("WEBSITE_NAME", opts.get("website_name", "Webseite")),
        "refresh_seconds": int(os.getenv("REFRESH_SECONDS", opts.get("refresh_seconds", 15))),
        "synology_host": os.getenv("SYNOLOGY_HOST", opts.get("synology_host", "")),
        "synology_port": int(os.getenv("SYNOLOGY_PORT", opts.get("synology_port", 5001))),
        "synology_https": str(os.getenv("SYNOLOGY_HTTPS", opts.get("synology_https", True))).lower()
                          not in ("0", "false", "no"),
        "synology_user": os.getenv("SYNOLOGY_USER", opts.get("synology_user", "")),
        "synology_password": os.getenv("SYNOLOGY_PASSWORD", opts.get("synology_password", "")),
        "synology_device_id": os.getenv("SYNOLOGY_DEVICE_ID", opts.get("synology_device_id", "")),
    }


OPT = load_options()
SUPERVISOR_TOKEN = os.getenv("SUPERVISOR_TOKEN", "")
SUP = "http://supervisor"
MOCK = os.getenv("HOMELAB_MOCK") == "1"

# Systemmonitor-Entities des HASS-Pi (bei Bedarf hier anpassen)
HA_SENSORS = {
    "cpu": "sensor.system_monitor_prozessornutzung",
    "temp": "sensor.system_monitor_prozessortemperatur",
    "mem": "sensor.system_monitor_arbeitsspeicherauslastung",
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

    # v1.1: Kernel, Prozesse, Swap, weitere Dateisysteme
    kernel = system.get("os_version") or system.get("platform")
    procs = (d.get("processcount", {}) or {}).get("total")
    swap = d.get("memswap", {}) or {}
    fslist = []
    for fs in d.get("fs", []) or []:
        mp = str(fs.get("mnt_point", ""))
        ft = str(fs.get("fs_type", "")).lower()
        if ft in ("tmpfs", "devtmpfs", "overlay", "squashfs", "") \
                or mp.startswith(("/boot", "/dev", "/sys", "/proc", "/run", "/snap")):
            continue
        fslist.append({"mount": mp, "pct": round(_num(fs.get("percent")), 1),
                       "used": _num(fs.get("used")), "total": _num(fs.get("size"))})
    fslist.sort(key=lambda x: x["total"], reverse=True)

    return {
        "ok": True,
        "label": label,
        "host": host,
        "hostname": system.get("hostname", host),
        "os": osname,
        "kernel": kernel,
        "uptime": up_str,
        "cpu_pct": round(_num(cpu.get("total")), 1),
        "temp_c": round(temp, 1) if temp is not None else None,
        "mem_pct": round(_num(mem.get("percent")), 1),
        "mem_used": _num(mem.get("used")),
        "mem_total": _num(mem.get("total")),
        "swap_pct": round(_num(swap.get("percent")), 1) if swap else None,
        "disk_pct": round(_num(root.get("percent")), 1),
        "disk_used": _num(root.get("used")),
        "disk_total": _num(root.get("size")),
        "filesystems": fslist[:4],
        "processes": int(procs) if procs is not None else None,
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
            "disk_used": _num(host.get("disk_used")) * 1024**3,
            "disk_total": _num(host.get("disk_total")) * 1024**3,
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


# ------------------------------------------------ Collector: Webseite ---------
async def collect_website(url: str, name: str) -> dict:
    """HTTP-Statuscheck einer Webseite: online/offline, HTTP-Code, Antwortzeit."""
    if not url:
        return {"ok": False, "error": "keine website_url gesetzt"}
    t0 = time.perf_counter()
    try:
        # verify=False: lokale/selbstsignierte Seiten sollen den Check nicht scheitern lassen
        async with httpx.AsyncClient(verify=False, follow_redirects=True) as c:
            r = await c.get(url, timeout=6.0)
        ms = round((time.perf_counter() - t0) * 1000)
        return {"ok": True, "name": name, "url": url,
                "up": 200 <= r.status_code < 400, "status": r.status_code, "ms": ms}
    except Exception as e:
        return {"ok": True, "name": name, "url": url,
                "up": False, "status": None, "ms": None, "error": str(e)}


# --------------------------------------------------- Collector: Synology DSM --
# DSM-Web-API: Login liefert eine Session-ID (sid). Ist 2FA erzwungen, muss der
# Benutzer einmalig ausserhalb dieses Codes per OTP + enable_device_token=yes
# freigeschaltet werden (siehe DOCS.md) - das Ergebnis ist eine device_id, die
# hier als synology_device_id dauerhaft eingetragen wird und OTP kuenftig erspart.
SYNOLOGY_DEVICE_NAME = "homelab-dashboard"
SYNOLOGY_FOLDERSCAN_SECONDS = 6 * 3600
SYNOLOGY_FOLDERSCAN_MAX_SHARES = 8
SYNOLOGY_FOLDERSCAN_MAX_SUBFOLDERS = 25
SYNOLOGY_FOLDERSCAN_TOP_N = 12
SYNOLOGY_SESSION_ERROR_CODES = (105, 106, 107, 119)  # ungueltige/abgelaufene Session

_syno_sid: dict = {"sid": None, "synotoken": None}
_syno_lock = asyncio.Lock()
_syno_folder_cache: dict = {"ok": False, "scanned_at": None, "top": [], "error": "noch nicht gescannt"}
_syno_last_error: str | None = None

# https://kb.synology.com/en-global/DSM/tutorial/What_is_HTTP_status_code_in_DSM_help
SYNOLOGY_AUTH_ERROR_TEXT = {
    400: "Benutzername oder Passwort falsch",
    401: "Konto deaktiviert",
    402: "Zugriff verweigert (Berechtigung fehlt, z. B. Anwendung 'DSM' nicht erlaubt)",
    403: "2FA-Code erforderlich",
    404: "2FA-Code falsch",
    406: "2FA ist fuer dieses Konto erzwungen, aber nicht eingerichtet",
    407: "Quell-IP von DSM blockiert (Systemsteuerung -> Sicherheit -> Schutz)",
    408: "Passwort abgelaufen und muss geaendert werden",
    409: "Passwort muss geaendert werden",
    410: "Passwort muss geaendert werden (erzwungen)",
}


def _syno_base(opt: dict) -> str:
    scheme = "https" if opt.get("synology_https", True) else "http"
    return f"{scheme}://{opt['synology_host']}:{opt['synology_port']}/webapi"


async def _syno_login(client: httpx.AsyncClient, opt: dict) -> dict | None:
    """Login mit Benutzer/Passwort + gemerkter device_id (kein OTP noetig, solange
    das Geraet in DSM als vertrauenswuerdig hinterlegt ist)."""
    global _syno_last_error
    params = {
        "api": "SYNO.API.Auth", "version": "6", "method": "login",
        "account": opt["synology_user"], "passwd": opt["synology_password"],
        "session": "homelab_dashboard", "format": "sid",
        "device_name": SYNOLOGY_DEVICE_NAME,
    }
    if opt.get("synology_device_id"):
        params["device_id"] = opt["synology_device_id"]
    url = f"{_syno_base(opt)}/entry.cgi"
    try:
        r = await client.get(url, params=params, timeout=8.0)
        r.raise_for_status()
        d = r.json()
    except Exception as e:
        _syno_last_error = f"Verbindung zu {url} fehlgeschlagen: {type(e).__name__}: {e}"
        return None
    if not d.get("success"):
        code = (d.get("error") or {}).get("code")
        text = SYNOLOGY_AUTH_ERROR_TEXT.get(code, "unbekannt")
        _syno_last_error = f"DSM-Login abgelehnt (Code {code}: {text})"
        return None
    data = d.get("data", {}) or {}
    _syno_last_error = None
    return {"sid": data.get("sid"), "synotoken": data.get("synotoken")}


async def _syno_get(client: httpx.AsyncClient, opt: dict, api: str, version: int,
                     method: str, extra: dict | None = None) -> dict:
    """Ruft eine DSM-Web-API auf; loggt bei Bedarf (erneut) ein. Wirft bei Fehlern."""
    async def _call(sid, token):
        params = {"api": api, "version": str(version), "method": method, "_sid": sid}
        if extra:
            params.update(extra)
        headers = {"X-SYNO-TOKEN": token} if token else {}
        r = await client.get(f"{_syno_base(opt)}/entry.cgi", params=params, headers=headers, timeout=10.0)
        r.raise_for_status()
        return r.json()

    async with _syno_lock:
        if not _syno_sid.get("sid"):
            _syno_sid.update(await _syno_login(client, opt) or {"sid": None})
    if not _syno_sid.get("sid"):
        detail = _syno_last_error or "unbekannter Fehler"
        raise RuntimeError(f"Synology-Login fehlgeschlagen: {detail}")

    d = await _call(_syno_sid["sid"], _syno_sid.get("synotoken"))
    if not d.get("success"):
        code = (d.get("error") or {}).get("code")
        if code in SYNOLOGY_SESSION_ERROR_CODES:
            async with _syno_lock:
                _syno_sid.update(await _syno_login(client, opt) or {"sid": None})
            if _syno_sid.get("sid"):
                d = await _call(_syno_sid["sid"], _syno_sid.get("synotoken"))
        if not d.get("success"):
            code = (d.get("error") or {}).get("code")
            raise RuntimeError(f"Synology-API-Fehler {api}.{method}: Code {code}")
    return d.get("data", {}) or {}


async def collect_synology(client: httpx.AsyncClient, opt: dict) -> dict:
    host = opt.get("synology_host")
    if not host or not opt.get("synology_user"):
        return {"ok": False, "error": "kein synology_host/synology_user gesetzt"}
    try:
        util, sysinfo, storage, procs, conns = await asyncio.gather(
            _syno_get(client, opt, "SYNO.Core.System.Utilization", 1, "get"),
            _syno_get(client, opt, "SYNO.Core.System", 1, "info", {"type": "storage"}),
            _syno_get(client, opt, "SYNO.Storage.CGI.Storage", 1, "load_info"),
            _syno_get(client, opt, "SYNO.Core.System.Process", 1, "list", {"additional": '["cpu","mem"]'}),
            _syno_get(client, opt, "SYNO.Core.CurrentConnection", 1, "list", {"offset": 0, "limit": 30}),
        )
    except Exception as e:
        return {"ok": False, "host": host, "error": str(e)}

    cpu = util.get("cpu", {}) or {}
    cpu_pct = None
    if cpu:
        cpu_pct = round(_num(cpu.get("user_load")) + _num(cpu.get("system_load"))
                        + _num(cpu.get("nice_load")), 1)

    mem = util.get("memory", {}) or {}
    mem_pct = round(_num(mem.get("real_usage") or mem.get("memory_usage")), 1) if mem else None

    net_list = util.get("network", []) or []
    rx = sum(_num(x.get("rx")) for x in net_list if str(x.get("device", "")).lower() not in ("total", ""))
    tx = sum(_num(x.get("tx")) for x in net_list if str(x.get("device", "")).lower() not in ("total", ""))

    volumes = []
    for v in (storage.get("volumes") or []):
        total = _num(v.get("total_size"))
        used = _num(v.get("used_size"))
        volumes.append({
            "id": v.get("id") or v.get("volume_path") or v.get("desc") or "?",
            "status": v.get("status", "unknown"),
            "used": used, "total": total,
            "pct": round(used / total * 100, 1) if total else None,
        })

    proc_list = []
    for p in sorted(procs.get("processes") or [],
                    key=lambda p: _num((p.get("additional") or {}).get("cpu")), reverse=True)[:6]:
        add = p.get("additional") or {}
        proc_list.append({"name": p.get("name") or "?", "cpu": round(_num(add.get("cpu")), 1)})

    users = []
    for c in (conns.get("items") or conns.get("connection") or []):
        users.append({
            "user": c.get("account") or c.get("user") or "?",
            "ip": c.get("address") or c.get("ip") or "",
            "proto": (c.get("type") or c.get("protocol") or "").upper(),
        })

    sys_temp = sysinfo.get("sys_temp")
    up_time = sysinfo.get("up_time")
    return {
        "ok": True,
        "host": host,
        "model": sysinfo.get("model"),
        "temp_c": round(_num(sys_temp), 1) if sys_temp not in (None, "") else None,
        "uptime": _fmt_uptime(_num(up_time)) if up_time else None,
        "cpu_pct": cpu_pct,
        "mem_pct": mem_pct,
        "net_rx": rx, "net_tx": tx,
        "volumes": volumes,
        "processes": proc_list,
        "users": users,
    }


# --------------------------------- Hintergrund-Job: groesste Ordner (TreeSize) -
async def _syno_list_shares(client: httpx.AsyncClient, opt: dict) -> list:
    d = await _syno_get(client, opt, "SYNO.FileStation.List", 2, "list_share", {"additional": "[]"})
    return [s for s in (d.get("shares") or []) if s.get("isdir")]


async def _syno_list_subfolders(client: httpx.AsyncClient, opt: dict, path: str) -> list:
    try:
        d = await _syno_get(client, opt, "SYNO.FileStation.List", 2, "list",
                            {"folder_path": path, "filetype": "dir", "additional": "[]"})
        return [f["path"] for f in (d.get("files") or []) if f.get("isdir")]
    except Exception:
        return []


async def _syno_dirsize(client: httpx.AsyncClient, opt: dict, path: str) -> float | None:
    """Startet SYNO.FileStation.DirSize fuer einen Ordner und wartet auf das Ergebnis
    (asynchroner DSM-Job, kann bei sehr grossen Ordnern eine Weile dauern)."""
    try:
        start = await _syno_get(client, opt, "SYNO.FileStation.DirSize", 2, "start",
                                {"path": json.dumps([path])})
        taskid = start.get("taskid")
        if not taskid:
            return None
        for _ in range(45):  # bis zu ~90s warten, alle 2s pollen
            await asyncio.sleep(2)
            st = await _syno_get(client, opt, "SYNO.FileStation.DirSize", 2, "status", {"taskid": taskid})
            if st.get("finished"):
                size = _num(st.get("total_size"), None)
                try:
                    await _syno_get(client, opt, "SYNO.FileStation.DirSize", 2, "stop", {"taskid": taskid})
                except Exception:
                    pass
                return size
        return None  # Timeout -> Ordner wird beim naechsten Scan erneut versucht
    except Exception:
        return None


async def _syno_scan_folders_once(opt: dict) -> None:
    if not opt.get("synology_host") or not opt.get("synology_user"):
        return
    async with httpx.AsyncClient(verify=False) as client:
        try:
            shares = (await _syno_list_shares(client, opt))[:SYNOLOGY_FOLDERSCAN_MAX_SHARES]
            entries: list[dict] = []
            sem = asyncio.Semaphore(3)

            async def size_of(path: str, label: str):
                async with sem:
                    s = await _syno_dirsize(client, opt, path)
                if s is not None:
                    entries.append({"path": path, "label": label, "size": s})

            # Ebene 1: die Freigaben selbst
            await asyncio.gather(*[size_of(sh["path"], sh["name"]) for sh in shares])

            # Ebene 2: direkte Unterordner jeder Freigabe
            level2 = []
            for sh in shares:
                subs = (await _syno_list_subfolders(client, opt, sh["path"]))[:SYNOLOGY_FOLDERSCAN_MAX_SUBFOLDERS]
                level2 += [(p, p.rsplit("/", 1)[-1]) for p in subs]
            await asyncio.gather(*[size_of(p, label) for p, label in level2])

            entries.sort(key=lambda e: e["size"], reverse=True)
            _syno_folder_cache.update({"ok": True, "error": None, "scanned_at": time.time(),
                                       "top": entries[:SYNOLOGY_FOLDERSCAN_TOP_N]})
        except Exception as e:
            _syno_folder_cache.update({"ok": False, "error": str(e), "scanned_at": time.time()})


async def _syno_folder_scan_loop() -> None:
    while True:
        try:
            await _syno_scan_folders_once(OPT)
        except Exception:
            pass
        await asyncio.sleep(SYNOLOGY_FOLDERSCAN_SECONDS)


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
                   "hostname": "ubuntu-srv", "os": "Ubuntu 24.04.4 LTS", "kernel": "6.8.0-40-generic",
                   "uptime": "21 T 6 Std 4 Min", "cpu_pct": 22.5, "temp_c": 51.0, "mem_pct": 63.0,
                   "mem_used": 10.1e9, "mem_total": 16e9, "swap_pct": 8.0, "disk_pct": 71.0,
                   "disk_used": 1.4e12, "disk_total": 2e12, "processes": 214,
                   "filesystems": [{"mount": "/", "pct": 71.0, "used": 1.4e12, "total": 2e12},
                                   {"mount": "/srv/fileserver", "pct": 46.0, "used": 3.7e12, "total": 8e12}],
                   "load": [1.45, 1.23, 0.98], "net_rx": 5.8e6, "net_tx": 1.2e6},
        "pihole": {"ok": True, "host": "192.168.1.5", "blocking": "enabled", "queries": 93157,
                   "blocked": 18342, "percent": 19.7, "gravity": 151284, "clients_active": 12,
                   "top_blocked": "graph.facebook.com", "top_client": "192.168.1.31"},
        "pihole_hw": {"ok": True, "label": "Pi-hole", "host": "192.168.1.5", "os": "Raspbian 11",
                      "kernel": "6.1.21-v8+", "uptime": "44 T 2 Std 9 Min", "cpu_pct": 3.5,
                      "temp_c": 47.2, "mem_pct": 18.7, "mem_used": 0.17e9, "mem_total": 0.92e9,
                      "swap_pct": 0.0, "disk_pct": 22.0, "disk_used": 6.4e9, "disk_total": 29e9,
                      "filesystems": [], "processes": 118, "load": [0.12, 0.15, 0.10],
                      "net_rx": 0.4e6, "net_tx": 0.3e6},
        "usage": {"ok": True, "session_pct": 61, "session_reset": now + 13440,
                  "weekly_pct": 11, "weekly_reset": now + 266400, "plan": "Max"},
        "website": {"ok": True, "name": "LaMetric-Uhr", "url": "http://192.168.1.7",
                    "up": True, "status": 200, "ms": 34},
        "synology": {"ok": True, "host": "192.168.178.25", "model": "DS920+",
                     "temp_c": 41.0, "uptime": "12 T 4 Std 30 Min", "cpu_pct": 7.5, "mem_pct": 68.0,
                     "net_rx": 1.4e6, "net_tx": 0.3e6,
                     "volumes": [{"id": "Volume 1", "status": "normal", "used": 2e12,
                                 "total": 2.7e12, "pct": 74.1}],
                     "processes": [{"name": "synoscgi", "cpu": 4.2}, {"name": "smbd", "cpu": 2.1},
                                   {"name": "python3", "cpu": 1.5}, {"name": "nginx", "cpu": 0.8},
                                   {"name": "synoindexd", "cpu": 0.4}],
                     "users": [{"user": "lutz", "ip": "192.168.178.44", "proto": "SMB"},
                               {"user": "lutz", "ip": "192.168.178.10", "proto": "DSM"}]},
        "synology_folders": {"ok": True, "error": None, "scanned_at": now - 3200,
                             "top": [{"path": "/volume1/backup", "label": "backup", "size": 8.1e11},
                                     {"path": "/volume1/homes/lutz/Downloads", "label": "Downloads",
                                      "size": 3.2e11},
                                     {"path": "/volume1/media", "label": "media", "size": 2.9e11},
                                     {"path": "/volume1/docker", "label": "docker", "size": 6.4e10}]},
        "ts": now,
    }


@app.on_event("startup")
async def _start_background_jobs():
    if not MOCK and OPT.get("synology_host") and OPT.get("synology_user"):
        asyncio.create_task(_syno_folder_scan_loop())


# ------------------------------------------------------------ Routen ----------
@app.get("/api/data")
async def api_data():
    if MOCK:
        return JSONResponse(_mock())
    async with httpx.AsyncClient(verify=False) as client:
        hass, ubuntu, pihole, pihole_hw, usage, website, synology = await asyncio.gather(
            collect_hass(client),
            collect_glances(client, OPT["ubuntu_host"], OPT["glances_port"], "Ubuntu-Server"),
            collect_pihole(client, OPT["pihole_host"], OPT["pihole_password"]),
            collect_glances(client, OPT["pihole_host"], OPT["glances_port"], "Pi-hole"),
            collect_usage(client, OPT["usage_url"]),
            collect_website(OPT["website_url"], OPT["website_name"]),
            collect_synology(client, OPT),
        )
    return JSONResponse({"hass": hass, "ubuntu": ubuntu, "pihole": pihole,
                         "pihole_hw": pihole_hw, "usage": usage, "website": website,
                         "synology": synology, "synology_folders": _syno_folder_cache,
                         "ts": time.time()})


@app.get("/api/config")
async def api_config():
    return {"refresh_seconds": OPT["refresh_seconds"]}


@app.get("/", response_class=HTMLResponse)
@app.get("/index.html", response_class=HTMLResponse)
async def index():
    return HTMLResponse((STATIC / "index.html").read_text(encoding="utf-8"))
