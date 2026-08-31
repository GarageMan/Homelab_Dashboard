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

### 2b. Exporter ablegen und als Dienst starten

Repo auf den Ubuntu-Server holen (öffentliches GitHub-Repo) und Exporter kopieren:

```bash
cd ~ && git clone https://github.com/<DEIN-USER>/Homelab_Dashboard.git
sudo mkdir -p /opt/claude-usage
sudo cp ~/Homelab_Dashboard/ubuntu-server/claude-usage-exporter.py /opt/claude-usage/
```

Dienst anlegen — er läuft als **dein Benutzer** und liest dessen
Anmeldedatei (`<benutzer>` = dein Login):

```bash
sudo tee /etc/systemd/system/claude-usage-exporter.service > /dev/null << 'EOF'
[Unit]
Description=Claude Usage Exporter (fuer Homelab Dashboard)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=<benutzer>
Environment=USAGE_PORT=8787
Environment=USAGE_TTL=90
Environment=CLAUDE_CRED=/home/<benutzer>/.claude/.credentials.json
ExecStart=/usr/bin/python3 /opt/claude-usage/claude-usage-exporter.py
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

# <benutzer> in der Datei durch deinen echten Login ersetzen, dann:
sudo systemctl daemon-reload
sudo systemctl enable --now claude-usage-exporter.service
sleep 2 ; curl -s http://localhost:8787/usage ; echo
```

Erwartung: `{"ok": true, "session_pct": ..., "weekly_pct": ...}`.

> **Token-Lebensdauer:** Der Login-Token wird nur erneuert, während Claude Code
> aktiv ist. Wird Claude Code auf dem Server lange gar nicht genutzt, kann er nach
> Stunden ablaufen — dann meldet `/usage` einen `HTTP 401`. Abhilfe: einmal
> `claude` auf dem Server ausführen, das frischt die Anmeldung auf.
>
> **Ausblick:** Ein dauerhaft gültiger Token wäre über `claude setup-token`
> möglich (ein Jahr gültig, als `CLAUDE_CODE_OAUTH_TOKEN` in einer geschützten
> `EnvironmentFile`). Das Herauskopieren dieses langen Tokens aus der Terminal-
> Oberfläche ist allerdings fehleranfällig; der Login-Token oben ist der
> zuverlässigere Weg für den Alltag.

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
   https://github.com/<DEIN-USER>/Homelab_Dashboard
   ```
   → **Hinzufügen** → schließen
3. Store neu laden (Seite aktualisieren). Im Abschnitt **„Homelab Add-ons"**
   erscheint **„Homelab Dashboard"** → anklicken → **Installieren**.
   Der Pi **baut das Image selbst** (`pip install` …) — das dauert **1–3 Minuten**.
4. Reiter **Konfiguration** → das Formular ist in Abschnitte gruppiert
   (Ubuntu-Server, Pi-hole, Webseite, Claude-Usage, Synology DiskStation),
   Werte auf die eigene Umgebung setzen:
   ```yaml
   refresh_seconds: 15
   glances_port: 61208
   ubuntu:
     host: IP-Ubuntu-FS
   pihole:
     host: IP-Pi-Hole
     password: "DEIN-PIHOLE-APP-PASSWORT"
   claude_usage:
     url: http://IP-Ubuntu-FS:8787/usage
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

## 5. Synology DiskStation (optional)

Zeigt CPU, RAM, Netzwerk, Temperatur, Volumes, die CPU-hungrigsten Prozesse und
angemeldete Benutzer der DiskStation — plus, alle 6 Stunden im Hintergrund neu
berechnet, die größten Ordner je Freigabe (2 Ebenen tief), ähnlich TreeSize.
Läuft komplett über die eingebaute DSM-Web-API, es muss nichts auf der
DiskStation installiert werden.

### 5a. Eigenen Benutzer in DSM anlegen

Die von DSM benötigten System-APIs (CPU/RAM/Storage/Prozesse) verlangen laut
Synologys eigener Dokumentation zwingend einen Benutzer aus der Gruppe
**„administrators"** — das lässt sich aber überall sonst einschränken:

1. **Systemsteuerung → Benutzer & Gruppe → Erstellen** → z. B. `dashboard-api`,
   eigenes Passwort, Gruppe **administrators**
2. Reiter **Anwendungen**: Zugriff auf **alles verweigern**, außer **File
   Station** (nur nötig für die Ordnergrößen-Funktion — ohne die reicht auch
   „alles verweigern")
3. Reiter **Freigabeordner**: pro Freigabe nur **Lesen**, nirgends **Schreiben**
4. Ist auf der DiskStation eine **2-Stufen-Verifizierung** für Administratoren
   erzwungen, greift sie auch für diesen Benutzer — weiter mit 5b.

### 5b. Einmalig: Geräte-Token für 2FA erzeugen

Nur nötig, wenn 2FA für Administratoren erzwungen ist. DSM kann ein Gerät
dauerhaft als vertrauenswürdig merken, sodass künftige Logins ganz ohne
OTP-Code auskommen. Von einem Rechner im selben LAN, mit dem **aktuellen**
6-stelligen OTP-Code aus der Authenticator-App:

```bash
curl -sk "https://IP-Synology:5001/webapi/entry.cgi?api=SYNO.API.Auth&version=6&method=login\
&account=dashboard-api&passwd=DEIN-PASSWORT&otp_code=123456\
&enable_device_token=yes&device_name=homelab-dashboard"
```

In der Antwort steht `"did":"..."` — dieser Wert ist die `device_id`. Kopieren
und im nächsten Schritt in die Add-on-Option **Synology DiskStation → Geräte-ID
(device_id)** eintragen.

### 5c. Add-on-Optionen setzen

```yaml
synology:
  host: IP-Synology
  port: 5001
  https: true
  user: dashboard-api
  password: "DEIN-PASSWORT"
  device_id: "DIE-DID-AUS-5b"      # leer lassen, falls kein 2FA erzwungen
```

Ist `synology.host` leer, blendet sich die Kachel komplett aus. Details und der
Funktionstest stehen ausführlicher im [Haupt-DOCS.md](../DOCS.md#5-synology-diskstation-optional)
im Repo-Wurzelverzeichnis.

---

## 6. Konfigurationsoptionen

Seit Version 1.3.0 ist das Konfigurationsformular in Abschnitte gruppiert.
Beim Umstieg von einer älteren Version müssen die Werte einmalig neu
eingetragen werden — Home Assistant übernimmt sie nicht automatisch aus den
alten, flachen Optionsnamen in die neuen Gruppen.

| Gruppe / Option              | Bedeutung                                         | Beispiel                       |
|-------------------------------|---------------------------------------------------|--------------------------------|
| `refresh_seconds`             | Aktualisierungsintervall des Dashboards (5–120 s) | `15`                           |
| `glances_port`                | Glances-Port auf Ubuntu-Server UND Pi-hole-Raspi  | `61208`                        |
| **Ubuntu-Server**             |                                                     |                                 |
| `ubuntu.host`                 | Adresse des Ubuntu-Servers (Glances)              | `IP-Ubuntu-FS`                 |
| **Pi-hole**                   |                                                     |                                 |
| `pihole.host`                 | Adresse des Pi-hole (Glances + Pi-hole-API)       | `IP-Pi-Hole`                   |
| `pihole.password`             | Pi-hole-**App**-Passwort                          | `••••••`                       |
| **Claude-Usage (optional)**   |                                                     |                                 |
| `claude_usage.url`            | URL des Claude-Usage-Exporters (leer = Kachel aus)| `http://IP-Ubuntu-FS:8787/usage` |
| **Synology DiskStation (optional)** |                                               |                                 |
| `synology.host`               | Adresse der DiskStation (leer = Kachel aus)       | `IP-Synology`                  |
| `synology.port`                | DSM-Port                                          | `5001`                         |
| `synology.https`               | DSM per HTTPS ansprechen                          | `true`                         |
| `synology.user`                | dedizierter API-Benutzer (Abschnitt 5a)           | `dashboard-api`                |
| `synology.password`            | Passwort dieses Benutzers                         | `••••••`                       |
| `synology.device_id`           | Geräte-Token bei erzwungenem 2FA (Abschnitt 5b)   | leer, falls kein 2FA           |

Alle Werte lassen sich jederzeit im Reiter **Konfiguration** ändern — kein
Rebuild nötig.

---

## 7. Fehlersuche

| Symptom | Ursache / Lösung |
|---|---|
| Add-on baut nicht | Add-on-**Protokoll** ansehen; für den `pip install`-Schritt braucht der Pi einmal Internet |
| Ubuntu/Pi-hole „nicht erreichbar" | Vom HASS-Pi aus `curl http://IP-…:61208/api/4/cpu` testen; Glances-Dienst und Firewall prüfen |
| CPU zeigt 0 % | Glances-4.4-Verhalten bei Leerlauf/Einzelabfrage — siehe Hinweis in Abschnitt 1; im Dashboard unkritisch |
| Pi-hole „auth fehlgeschlagen" | App-Passwort falsch/leer; in den Add-on-Optionen korrigieren |
| HASS-Kachel ohne CPU/Temp/RAM | Systemmonitor-Entitäten aktiviert? Exakte IDs in `HA_SENSORS` eingetragen? (Abschnitt 3) |
| HASS-Storage in „B" statt „GB" | Vor v1.0.1; auf aktuelle Add-on-Version aktualisieren |
| Claude Usage „nicht erreichbar" | Exporter-Dienst läuft? `systemctl status claude-usage-exporter` |
| Claude Usage `HTTP 401` | Login-Token abgelaufen → einmal `claude` auf dem Ubuntu-Server ausführen |
| Synology-Kachel fehlt | `synology.host` ist leer — sobald gesetzt, erscheint die Kachel |
| Synology „Login fehlgeschlagen" | Benutzer/Passwort falsch, oder 2FA erzwungen ohne gültige `synology_device_id` (Abschnitt 5b) |
| Synology „Größte Ordner": kein Scan | Läuft erst 6 h nach Add-on-Start und braucht `synology_user` mit File-Station-Zugriff (Abschnitt 5a) |

---

## Versionshinweise

- **1.3.1** — Top-Prozesse und angemeldete Benutzer der Synology-Kachel zeigten mangels korrekter DSM-Feldnamen keine Daten ("keine Daten" / "?"). Behoben: Prozessliste steht unter `process` mit `command`/`cpu` direkt auf oberster Ebene, Benutzername/Quell-IP der aktuellen Verbindungen unter `who`/`from` statt `account`/`address`.
- **1.3.0** — Konfigurationsformular in Abschnitte gruppiert (Ubuntu-Server,
  Pi-hole, Webseite, Claude-Usage, Synology DiskStation) statt einer langen
  flachen Liste; deutsche und englische Feldbeschriftungen/-beschreibungen
  über `translations/`. **Achtung:** bestehende Werte müssen nach dem Update
  einmalig neu eingetragen werden (siehe Abschnitt 6).
- **1.2.1** — Die Synology-Kachel zeigt bei Verbindungs-/Login-Problemen jetzt den
  tatsächlichen Grund an (Verbindungsfehler oder konkreter DSM-Fehlercode mit
  Klartext, z. B. „Zugriff verweigert" oder „Quell-IP blockiert") statt einer
  generischen Meldung.
- **1.2.0** — Neue Synology-DiskStation-Kachel: CPU/RAM/Netzwerk/Temperatur/Volumes,
  Top-CPU-Prozesse, angemeldete Benutzer, plus ein alle 6 h laufender
  Hintergrund-Scan der größten Ordner je Freigabe (ähnlich TreeSize). Siehe
  Abschnitt 5.
- **1.0.1** — HASS-Pi-Storage korrekt in GB statt Bytes; Dokumentation an reale
  Umgebung angepasst (venv/pipx-Glances, Login-Token-Exporter, sprachabhängige
  Systemmonitor-IDs, deutsche Menüpfade).
- **1.0.0** — Erstveröffentlichung.
