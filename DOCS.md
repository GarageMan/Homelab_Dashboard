# Homelab Dashboard — Installation & Betrieb

Ein Home-Assistant-Add-on, das drei Server auf einen Blick zusammenfasst —
**HASS-Pi**, **Ubuntu-Server** und **Pi-hole** — plus eine **Claude-Usage**-Kachel.
Es läuft als Ingress-Panel direkt in der Home-Assistant-Seitenleiste
(HA-Login, kein offener Port).

---

## Platzhalter für IP-Adressen

In dieser Anleitung stehen **Platzhalter** statt echter IP-Adressen. Ersetze sie
überall durch die Werte deiner Umgebung:

| Platzhalter        | Bedeutung                                             |
|--------------------|-------------------------------------------------------|
| `IP-Homeassistant` | der HAOS-Raspberry-Pi (auf dem das Add-on läuft)      |
| `IP-Ubuntu-FS`     | der Ubuntu-Server (Fileserver / DayZ / Kleinigkeiten) |
| `IP-Pi-Hole`       | der Pi-hole-Server                                    |
| `<benutzer>`       | dein Linux-Benutzername auf dem jeweiligen Server     |

Beispiel: Steht in der Anleitung `http://IP-Ubuntu-FS:61208`, trägst du die echte
Adresse deines Ubuntu-Servers ein.

---

## Überblick / Datenquellen

| Server                | Allgemeine Metriken | Spezifisches                          |
|-----------------------|---------------------|---------------------------------------|
| HASS-Pi               | Supervisor- + Core-API (im Add-on ohne Zusatzkonfiguration) + Systemmonitor-Sensoren | HA-Version, Updates, Entitäten-Health |
| Ubuntu-Server         | Glances-REST-API (Port 61208) | —                          |
| Pi-hole               | Glances-REST-API (Port 61208) | Pi-hole-v6-API (Queries, Blocking …) |
| Claude Usage          | Exporter auf dem Ubuntu-Server (Port 8787) | Session-/Weekly-Auslastung |

Ein FastAPI-Aggregator im Add-on fragt alle Quellen parallel und gekapselt ab;
fällt eine aus, zeigt nur ihre Kachel „nicht erreichbar", das Board bleibt stehen.

---

## 1. Glances auf Ubuntu-Server **und** Pi-hole

Auf **beiden** Linux-Servern (`IP-Ubuntu-FS` und `IP-Pi-Hole`) installieren.
Glances 4 stellt im Web-/API-Modus (`-w`) alle allgemeinen Metriken über eine
REST-API bereit.

### Installation

Zwei Wege — der **venv-Weg funktioniert überall** (auch auf älterem Raspbian
Bullseye mit Python 3.9) und ist daher die sichere Wahl:

```bash
# --- Weg A: venv (universell) ---
sudo apt update && sudo apt install -y python3-venv lm-sensors
python3 -m venv ~/glances-venv
~/glances-venv/bin/pip install --upgrade pip
~/glances-venv/bin/pip install 'glances[web]'
~/glances-venv/bin/glances --version        # Pfad merken: ~/glances-venv/bin/glances
```

```bash
# --- Weg B: pipx (nur auf Systemen mit AKTUELLEM pipx, z. B. Ubuntu 24.04) ---
sudo apt install -y pipx lm-sensors
pipx ensurepath
pipx install 'glances[web]'
which glances                                # Pfad merken: meist ~/.local/bin/glances
```

> **Achtung Raspbian Bullseye (Pi-hole):** Das `pipx` aus den apt-Paketquellen ist
> dort uralt (v0.12) und bricht mit `TypeError: __init__() got an unexpected
> keyword argument 'encoding'` ab. Nimm auf diesem Gerät **Weg A (venv)** — oder
> aktualisiere pipx zuerst mit `sudo apt remove -y pipx && python3 -m pip install
> --user pipx`.

`sensors-detect --auto` (einmalig, mit `sudo`) hilft, damit Glances die
CPU-Temperatur findet.

### Als Dienst einrichten

Der `ExecStart`-Pfad muss auf dein tatsächliches Glances zeigen (venv **oder**
pipx — siehe oben). `$USER` und `$HOME` setzt die Shell beim Anlegen automatisch:

```bash
sudo tee /etc/systemd/system/glances-web.service > /dev/null << EOF
[Unit]
Description=Glances (Web/REST-API) fuer Homelab Dashboard
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$USER
# venv:  $HOME/glances-venv/bin/glances
# pipx:  $HOME/.local/bin/glances
ExecStart=$HOME/glances-venv/bin/glances -w -t 5
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now glances-web.service
systemctl status glances-web.service --no-pager
```

### Funktionstest

```bash
curl -s http://localhost:61208/api/4/cpu | grep -o '"total":[^,]*'
```

> **Wichtig zu den CPU-Werten (Glances 4.4):** Eine **Einzelabfrage** kurz nach
> dem Start (oder auf einem Leerlauf-System) zeigt `"total": 0.0` — das ist
> normal. CPU-Prozente werden über ein Intervall berechnet und brauchen zwei
> Messungen mit **> 5 s** Abstand. Testen mit etwas Last:
> ```bash
> timeout 25 yes >/dev/null &
> for i in 1 2 3; do sleep 8; curl -s http://localhost:61208/api/4/cpu | grep -o '"total":[^,]*'; done
> ```
> Die Werte sollten ansteigen. Fürs Dashboard ist das unkritisch — es fragt alle
> 15 s ab, also immer mit frisch berechneten Werten.

### Absicherung (optional)

Glances lauscht ohne Passwort auf allen Interfaces — im vertrauenswürdigen
Heim-LAN in Ordnung. Enger ziehen per Firewall auf die HASS-Pi-IP:

```bash
sudo ufw allow from IP-Homeassistant to any port 61208 proto tcp
```

---

## 2. Claude-Usage-Exporter (nur Ubuntu-Server)

Der Exporter liest die kontoweite Auslastung (Session 5 h / Weekly 7 d) über
einen minimalen API-Call und stellt sie als JSON bereit. Er nutzt die Anmeldung
von **Claude Code**.

### 2a. Claude Code installieren und anmelden

```bash
echo "$ANTHROPIC_API_KEY"        # sollte LEER sein (sonst metered statt Abo)
curl -fsSL https://claude.ai/install.sh | bash
# neue Shell / source ~/.bashrc, dann:
claude
```

Beim ersten Start durch den Browser-Login gehen (URL öffnen, einloggen, kurzen
Code zurück ins Terminal). Danach existiert `~/.claude/.credentials.json`.
Kurz mit `/status` prüfen (zeigt Abo + Limits), dann `/exit`.

### 2b. Exporter ablegen

Repo auf den Ubuntu-Server holen und Exporter kopieren:

```bash
cd ~ && git clone https://github.com/GarageMan/Homelab_Dashboard.git
sudo mkdir -p /opt/claude-usage
sudo cp ~/Homelab_Dashboard/ubuntu-server/claude-usage-exporter.py /opt/claude-usage/
```

### 2c. Langlebigen Token erzeugen (ein Jahr gültig)

Der Login-Access-Token aus 2a lebt nur wenige Stunden. Für den Dauerbetrieb erzeugt
`claude setup-token` einen **ein Jahr gültigen** Token. Weil das Terminal lange
Pasten verstümmelt, den Token **über eine Datei** übernehmen, nicht über die
Kommandozeile:

```bash
claude setup-token                 # ausgegebenen sk-ant-oat…-Token kopieren
nano ~/ct.txt                      # Token (nur ihn, eine Zeile) einfügen, speichern
TOK=$(tr -d ' \t\r\n' < ~/ct.txt)
echo "Länge: ${#TOK}"              # ~108 = vollständig; deutlich kürzer = abgeschnitten
```

Token in eine geschützte Datei schreiben (außerhalb des Repos):

```bash
printf 'CLAUDE_CODE_OAUTH_TOKEN=%s\n' "$TOK" | sudo tee /etc/claude-usage/token.env >/dev/null
sudo chmod 600 /etc/claude-usage/token.env
unset TOK ; rm -f ~/ct.txt
```

### 2d. Dienst anlegen

Der Dienst läuft als **dein Benutzer** und liest den Token aus der geschützten Datei
(`<benutzer>` = dein Login):

```bash
sudo tee /etc/systemd/system/claude-usage-exporter.service > /dev/null << 'EOF'
[Unit]
Description=Claude Usage Exporter (fuer Homelab Dashboard)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=<benutzer>
EnvironmentFile=/etc/claude-usage/token.env
Environment=USAGE_PORT=8787
Environment=USAGE_TTL=90
ExecStart=/usr/bin/python3 /opt/claude-usage/claude-usage-exporter.py
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

# <benutzer> durch deinen echten Login ersetzen, dann:
sudo systemctl daemon-reload
sudo systemctl enable --now claude-usage-exporter.service
sleep 2 ; curl -s http://localhost:8787/usage ; echo
```

Erwartung: `{"ok": true, "session_pct": ..., "weekly_pct": ...}`.

> **Token-Erneuerung:** Der setup-token gilt ~1 Jahr. Läuft er ab (Kachel zeigt
> `HTTP 401 … expired`), die Schritt-für-Schritt-Routine in
> **`ubuntu-server/Claude-Usage-Token-erneuern.md`** durchgehen. Der Exporter nutzt
> `CLAUDE_CODE_OAUTH_TOKEN` bevorzugt und fällt nur ersatzweise auf
> `~/.claude/.credentials.json` zurück (Login-Token aus 2a).

---

## 3. HASS-Pi: Systemmonitor-Integration aktivieren

Liefert CPU-Last, CPU-Temperatur und RAM des HASS-Pi selbst (Werte für die
HASS-Kachel). Läuft komplett in der HA-Oberfläche.

1. **Einstellungen → Geräte & Dienste → „+ Integration hinzufügen" → „System Monitor"**
2. **Wichtig:** Alle Entitäten der Integration sind **standardmäßig deaktiviert**
   und als „Diagnose" markiert — deaktivierte Entitäten erscheinen **weder** in
   Entwicklerwerkzeuge → Zustände **noch** in der normalen Entitätssuche. Du musst
   die benötigten erst aktivieren: Integration öffnen → Gerät „System Monitor" →
   Entität anklicken → Zahnrad → **Aktiviert** einschalten → Aktualisieren. Für:
   - **Processor use** (CPU-Last, %)
   - **Memory use** in **%** (nicht die MiB-Variante!)
   - **Processor temperature** (falls vorhanden — siehe Hinweis)

3. Danach in **Entwicklerwerkzeuge → Zustände** die **exakten** Entitäts-IDs
   ablesen. Sie hängen von der **Sprache** der Oberfläche ab. Beispiel bei
   deutscher Oberfläche:
   - CPU-Last → `sensor.system_monitor_prozessornutzung`
   - CPU-Temp → `sensor.system_monitor_prozessortemperatur`
   - RAM %   → `sensor.system_monitor_arbeitsspeicherauslastung`

4. Diese IDs in `homelab_dashboard/app/main.py` im Block `HA_SENSORS = {` eintragen
   (Schlüssel `cpu`, `temp`, `mem`) und committen.

> **CPU-Temperatur:** Ist in virtualisierten/Container-Umgebungen kein
> Hardware-Temperatursensor verfügbar, wird die Temperatur-Entität gar nicht
> angelegt — dann bleibt im Dashboard nur die Temperaturzeile leer, CPU und RAM
> funktionieren trotzdem. (Auf vielen Raspberry-Pi-HAOS-Systemen ist die
> Temperatur aber vorhanden.)
>
> **„Unbekannt" direkt nach dem Aktivieren:** Die CPU-Last steht anfangs kurz auf
> „Unbekannt", bis die erste Intervall-Messung vorliegt — nach 1–2 Minuten
> erscheint der Wert.

---

## 4. Das Add-on installieren

Das Repo enthält eine `repository.yaml` und ist damit ein Add-on-Repository —
Einbindung per URL, kein Dateikopieren nötig. (Menü-Beschriftungen der **deutschen**
HA-Oberfläche.)

1. **Einstellungen → Apps → App installieren** (der Add-on-Store)
2. Oben rechts **⋮ → Repositories** → **„+ Hinzufügen"** → URL einfügen:
   ```
   https://github.com/GarageMan/Homelab_Dashboard
   ```
   → **Hinzufügen** → schließen
3. Store neu laden (Seite aktualisieren). Im Abschnitt **„Homelab Add-ons"**
   erscheint **„Homelab Dashboard"** → anklicken → **Installieren**.
   Der Pi **baut das Image selbst** (`pip install` …) — das dauert **1–3 Minuten**.
4. Reiter **Konfiguration** → Werte auf die eigene Umgebung setzen:
   ```yaml
   ubuntu_host: IP-Ubuntu-FS
   pihole_host: IP-Pi-Hole
   glances_port: 61208
   pihole_password: "DEIN-PIHOLE-APP-PASSWORT"
   usage_url: http://IP-Ubuntu-FS:8787/usage
   website_url: http://IP-Homeassistant
   website_name: "Meine Webseite"
   refresh_seconds: 15
   ```
5. Reiter **Info** → **Starten** → **„In Seitenleiste anzeigen"** aktivieren.
   Nach F5 erscheint **„Homelab"** in der Seitenleiste → öffnen.

> **Pi-hole-App-Passwort:** In der Pi-hole-Oberfläche unter **Settings → Web
> interface / API → App password** erzeugen (nicht das normale Login-Passwort).

### Updates des Add-ons

Nach Änderungen im Repo die `version` in `homelab_dashboard/config.yaml` erhöhen
(z. B. `1.0.1`) und committen. In HA: **Apps → ⋮ → Nach Updates suchen** → beim
Add-on **Aktualisieren** → **Starten**.

---

## 5. Konfigurationsoptionen

| Option            | Bedeutung                                         | Beispiel                       |
|-------------------|---------------------------------------------------|--------------------------------|
| `ubuntu_host`     | Adresse des Ubuntu-Servers (Glances)              | `IP-Ubuntu-FS`                 |
| `pihole_host`     | Adresse des Pi-hole (Glances + Pi-hole-API)       | `IP-Pi-Hole`                   |
| `glances_port`    | Glances-Port auf beiden Servern                   | `61208`                        |
| `pihole_password` | Pi-hole-**App**-Passwort                          | `••••••`                       |
| `usage_url`       | URL des Claude-Usage-Exporters                    | `http://IP-Ubuntu-FS:8787/usage` |
| `website_url`     | URL für den Webseiten-Status (leer = Kachel aus)  | `http://IP-Homeassistant`      |
| `website_name`    | Anzeigename der Webseiten-Kachel                  | `Meine Webseite`               |
| `refresh_seconds` | Aktualisierungsintervall des Dashboards (5–120 s) | `15`                           |

Alle Werte lassen sich jederzeit im Reiter **Konfiguration** ändern — kein
Rebuild nötig.

---

## 6. Fehlersuche

| Symptom | Ursache / Lösung |
|---|---|
| Add-on baut nicht | Add-on-**Protokoll** ansehen; für den `pip install`-Schritt braucht der Pi einmal Internet |
| Ubuntu/Pi-hole „nicht erreichbar" | Vom HASS-Pi aus `curl http://IP-…:61208/api/4/cpu` testen; Glances-Dienst und Firewall prüfen |
| CPU zeigt 0 % | Glances-4.4-Verhalten bei Leerlauf/Einzelabfrage — siehe Hinweis in Abschnitt 1; im Dashboard unkritisch |
| Pi-hole „auth fehlgeschlagen" | App-Passwort falsch/leer; in den Add-on-Optionen korrigieren |
| HASS-Kachel ohne CPU/Temp/RAM | Systemmonitor-Entitäten aktiviert? Exakte IDs in `HA_SENSORS` eingetragen? (Abschnitt 3) |
| HASS-Storage in „B" statt „GB" | Vor v1.0.1; auf aktuelle Add-on-Version aktualisieren |
| Claude Usage „nicht erreichbar" | Exporter-Dienst läuft? `systemctl status claude-usage-exporter` |
| Claude Usage `HTTP 401 … expired` | setup-token abgelaufen (~1 Jahr) → Routine `Claude-Usage-Token-erneuern.md` |
| Webseiten-Kachel fehlt | `website_url` ist leer — sobald gesetzt, erscheint die Kachel |

---

## Versionshinweise

- **1.1.1** — Webseiten-Status-Kachel (Online/Offline, HTTP-Code, Antwortzeit) über
  `website_url`/`website_name`.
- **1.1.0** — Pi-hole-Kachel zeigt zusätzlich die Hardware des Pi-hole-Raspi
  (CPU/RAM/Storage/Temp/Uptime, aus dem dort laufenden Glances); Ubuntu-Kachel um
  Kernel, Prozesse, Swap und weitere Volumes erweitert; „Gesundheit" umbenannt in
  „unavailable/unknown entities"; Refresh-Button. Claude-Usage auf langlebigen
  `setup-token` umgestellt.
- **1.0.1** — HASS-Pi-Storage korrekt in GB statt Bytes; Dokumentation an reale
  Umgebung angepasst (venv/pipx-Glances, Login-Token-Exporter, sprachabhängige
  Systemmonitor-IDs, deutsche Menüpfade).
- **1.0.0** — Erstveröffentlichung.
