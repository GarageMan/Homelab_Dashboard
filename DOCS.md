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
| `IP-Synology`      | deine Synology DiskStation                            |
| `IP-Fritzbox`      | deine FritzBox (z. B. `fritz.box` oder ihre feste IP) |
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
| Synology DiskStation  | DSM-Web-API direkt (Port 5001, https) | CPU/RAM/Netz/Temp/Volumes, Top-Prozesse, angemeldete Benutzer, größte Ordner (Hintergrund-Scan) |
| FritzBox              | TR-064 (`fritzconnection`) | Geräteliste (aktiv/gesamt, ausklappbar), WAN-Traffic rauf/runter, Sync-Speed, externe IP |

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
   website:
     url: http://IP-Homeassistant
     name: "Meine Webseite"
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
2. Reiter **Anwendungen**: **„DSM" muss erlaubt bleiben** — DSMs Web-GUI-Login
   läuft über genau dieselbe API wie der Login des Dashboards, ein verweigertes
   „DSM" blockiert also **beide** (nicht nur die Web-Oberfläche). Alles andere
   auf **verweigern**, außer **File Station** (nur für die Ordnergrößen-Funktion
   nötig — ohne die reicht „DSM" allein). Weil dieser Benutzer zwingend in der
   Gruppe „administrators" ist *und* „DSM" erlaubt sein muss, hat ein
   kompromittiertes Passwort vollen Admin-Zugriff auf die Web-Oberfläche — als
   Ausgleich in derselben Zeile bei „DSM" auf **„Nach IP-Adresse"** klicken und
   nur die IP des Home-Assistant-Geräts eintragen, dann funktioniert der Login
   ausschließlich von dort aus.
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
Ab dann meldet sich das Dashboard mit Benutzer/Passwort + `device_id` an, ohne
je wieder nach einem OTP-Code zu fragen (bis du das Gerät in DSM unter
**Systemsteuerung → Benutzer & Gruppe → [Benutzer] → Geräteverwaltung**
wieder entfernst).

### 5c. Add-on-Optionen setzen

Im Reiter **Konfiguration** des Add-ons:

```yaml
synology:
  host: IP-Synology
  port: 5001
  https: true
  user: dashboard-api
  password: "DEIN-PASSWORT"
  device_id: "DIE-DID-AUS-5b"      # leer lassen, falls kein 2FA erzwungen
```

Ist `synology.host` leer, blendet sich die Kachel komplett aus.

### 5d. Funktionstest

```bash
curl -sk "https://IP-Synology:5001/webapi/entry.cgi?api=SYNO.Core.System.Utilization&version=1&method=get&_sid=DEINE_SID"
```

(`_sid` aus der Antwort von 5b/Login.) Kommt eine plausible JSON-Antwort mit
`cpu`/`memory`/`network` zurück, stimmen Host/Port/Zugangsdaten. Bricht die
Kachel im Dashboard trotzdem mit „nicht erreichbar" ab, im Add-on-Protokoll
nachsehen — die Fehlermeldung enthält die betroffene API und den DSM-Fehlercode.

> **Hinweis:** Die JSON-Feldnamen der DSM-API (z. B. `sys_temp`, `real_usage`)
> können sich zwischen DSM-Versionen leicht unterscheiden. Zeigt eine einzelne
> Zeile dauerhaft „–" statt eines Werts, obwohl die Kachel grundsätzlich lädt,
> curl testweise gegen die jeweilige API laufen lassen (wie oben) und die
> tatsächlichen Feldnamen mit denen in `collect_synology()` in `app/main.py`
> abgleichen.

---

## 6. FritzBox (optional)

Liest Geräteliste (aktiv/gesamt) und WAN-Traffic per **TR-064** — dem lokalen
SOAP-Protokoll der FritzBox — über die Python-Bibliothek `fritzconnection`.
Kein Cloud-Umweg, keine MyFritz-Anmeldung nötig.

> **Kein Traffic pro Gerät:** TR-064 liefert nur den **gesamten** WAN-Traffic
> der FritzBox, keinen Traffic-Zähler pro einzelnem Client.

### 6a. Dedizierten FritzBox-Benutzer anlegen

Eigenes Konto statt des Admin-Logins verwenden:

**FritzBox-Oberfläche → System → FritzBox-Benutzer → Benutzer hinzufügen**

- Benutzername z. B. `dashboard-api`, eigenes Passwort vergeben
- Berechtigung **„FRITZ!Box Einstellungen"** aktivieren (Pflicht für TR-064)
- Alle anderen Berechtigungen (Sprachnachrichten, Smart Home, NAS-Inhalte …)
  können deaktiviert bleiben

### 6b. Zugriff für Anwendungen erlauben

TR-064 ist standardmäßig aus. Aktivieren unter:

**Heimnetz → Netzwerk → Netzwerkeinstellungen → „Zugriff für Anwendungen
erlauben"** (Haken setzen, speichern)

Ohne diesen Haken schlägt jeder TR-064-Zugriff fehl, unabhängig von
Benutzername/Passwort.

### 6c. Add-on-Optionen setzen

Im Reiter **Konfiguration** des Add-ons:

```yaml
fritzbox:
  host: IP-Fritzbox
  user: dashboard-api
  password: "DEIN-PASSWORT"
```

Ist `fritzbox.host` leer, blendet sich die Kachel komplett aus.

### 6d. Funktionstest

```bash
curl -s "http://IP-Fritzbox:49000/tr64desc.xml" | head -5
```

Kommt XML zurück, ist TR-064 grundsätzlich erreichbar. Bricht die Kachel im
Dashboard trotzdem ab, im Add-on-**Protokoll** nachsehen — die Fehlermeldung
von `fritzconnection` enthält meist den genauen Grund (z. B. „401 Unauthorized"
bei falschem Benutzer/Passwort, oder eine fehlende Berechtigung).

> **Geräteliste erscheint erst nach ~1 Minute:** Sie wird — anders als Traffic
> und Status — nicht bei jedem 15s-Poll neu geholt, sondern alle 60 Sekunden im
> Hintergrund gescannt (ein TR-064-Call **pro bekanntem Gerät** wäre bei jedem
> Poll spürbar langsam und unnötige Last auf der FritzBox). Direkt nach dem
> Add-on-Start zeigt die Kachel deshalb kurz „noch nicht gescannt".
>
> **PPPoE/DSL-Anschlüsse:** Verbindungsstatus und externe IP nutzen einen
> TR-064-Dienst, der je nach Anschlussart (PPPoE vs. IP-basiert, z. B. Kabel)
> auf manchen FritzBox-Modellen fehlt. Fehlt er, bleiben genau diese beiden
> Zeilen leer — Traffic-Werte und Geräteliste sind davon unabhängig und
> funktionieren in jedem Fall.

---

## 7. Konfigurationsoptionen

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
| **Webseite (optional)**       |                                                     |                                 |
| `website.url`                 | URL für den Webseiten-Status (leer = Kachel aus)  | `http://IP-Homeassistant`      |
| `website.name`                | Anzeigename der Webseiten-Kachel                  | `Meine Webseite`               |
| **Claude-Usage (optional)**   |                                                     |                                 |
| `claude_usage.url`            | URL des Claude-Usage-Exporters (leer = Kachel aus)| `http://IP-Ubuntu-FS:8787/usage` |
| **Synology DiskStation (optional)** |                                               |                                 |
| `synology.host`               | Adresse der DiskStation (leer = Kachel aus)       | `IP-Synology`                  |
| `synology.port`                | DSM-Port                                          | `5001`                         |
| `synology.https`               | DSM per HTTPS ansprechen                          | `true`                         |
| `synology.user`                | dedizierter API-Benutzer (Abschnitt 5a)           | `dashboard-api`                |
| `synology.password`            | Passwort dieses Benutzers                         | `••••••`                       |
| `synology.device_id`           | Geräte-Token bei erzwungenem 2FA (Abschnitt 5b)   | leer, falls kein 2FA           |
| **FritzBox (optional)**       |                                                     |                                 |
| `fritzbox.host`               | Adresse der FritzBox (leer = Kachel aus)          | `IP-Fritzbox`                  |
| `fritzbox.user`               | dedizierter FritzBox-Benutzer (Abschnitt 6a)      | `dashboard-api`                |
| `fritzbox.password`           | Passwort dieses Benutzers                         | `••••••`                       |

Alle Werte lassen sich jederzeit im Reiter **Konfiguration** ändern — kein
Rebuild nötig.

---

## 8. Fehlersuche

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
| Webseiten-Kachel fehlt | `website.url` ist leer — sobald gesetzt, erscheint die Kachel |
| Synology-Kachel fehlt | `synology.host` ist leer — sobald gesetzt, erscheint die Kachel |
| Synology „Login fehlgeschlagen" | Benutzer/Passwort falsch, oder 2FA erzwungen ohne gültige `synology.device_id` (Abschnitt 5b) |
| Synology-API-Fehler Code 105/106/107/119 | Session abgelaufen — wird beim nächsten Poll automatisch neu eingeloggt; bleibt es bestehen, `synology.password`/`synology.device_id` prüfen |
| Synology „Größte Ordner": kein Scan | Läuft erst 6 h nach Add-on-Start und braucht `synology_user` mit File-Station-Zugriff (Abschnitt 5a) |
| Einzelne Synology-Zeile zeigt „–" | DSM-JSON-Feldname weicht ab — siehe Hinweis in Abschnitt 5d |
| FritzBox-Kachel fehlt | `fritzbox.host` ist leer — sobald gesetzt, erscheint die Kachel |
| FritzBox „401 Unauthorized" / Verbindung fehlgeschlagen | Benutzer/Passwort falsch, oder Berechtigung „FRITZ!Box Einstellungen" fehlt (Abschnitt 6a) |
| FritzBox: alle Felder leer trotz „ok" | „Zugriff für Anwendungen erlauben" nicht aktiviert (Abschnitt 6b) |
| FritzBox: nur Status/externe IP fehlen, Traffic da | Anschlussart-bedingte TR-064-Einschränkung — siehe Hinweis am Ende von Abschnitt 6d, unkritisch |
| FritzBox-Geräteliste zeigt „noch nicht gescannt" | Scan läuft alle 60 s im Hintergrund, direkt nach Add-on-Start kurz normal |

---

## Versionshinweise

- **1.4.0** — Neue FritzBox-Kachel: Geräteliste (aktiv/gesamt, ausklappbar mit
  Name/IP/Verbindungsart) und WAN-Traffic (rauf/runter, Gesamt seit
  Verbindung, Sync-Speed, externe IP) per TR-064 (`fritzconnection`). Die
  Geräteliste läuft — anders als Traffic/Status — alle 60 s im Hintergrund,
  um die FritzBox bei vielen bekannten Geräten nicht mit einem SOAP-Call pro
  Gerät bei jedem 15s-Poll zu belasten. Konfiguration über
  `fritzbox.host`/`.user`/`.password` (siehe Abschnitt 6).
- **Doku-Fix** — Abschnitt 5a korrigiert: Die Anwendung „DSM" muss beim
  dedizierten Benutzer erlaubt bleiben (nicht verweigert werden), sonst
  scheitert sowohl der GUI- als auch der API-Login, da beide dieselbe
  DSM-Auth-API nutzen. Als Ausgleich fuer die dadurch noetige
  Admin-Berechtigung: „DSM" per „Nach IP-Adresse" auf die IP des
  Home-Assistant-Geraets einschraenken.
- **1.3.2** — "Top-Prozesse" filtert jetzt `synoscgi_*`-Eintraege heraus (das sind DSM-eigene, kurzlebige Prozesse fuer die eigenen API-Aufrufe des Dashboards inkl. Ordner-Scan) - vorher dominierten die sich selbst statt echter Last wie z. B. Indexierung oder Backup. Hinweis: CPU-Werte >100 % pro Prozess sind normal (ein Kern = 100 %, Mehrkern-/Multithread-Prozesse koennen mehr anzeigen).
- **1.3.1** — Top-Prozesse und angemeldete Benutzer der Synology-Kachel zeigten mangels korrekter DSM-Feldnamen keine Daten ("keine Daten" / "?"). Behoben: Prozessliste steht unter `process` mit `command`/`cpu` direkt auf oberster Ebene, Benutzername/Quell-IP der aktuellen Verbindungen unter `who`/`from` statt `account`/`address`.
- **1.3.0** — Konfigurationsformular in Abschnitte gruppiert (Ubuntu-Server,
  Pi-hole, Webseite, Claude-Usage, Synology DiskStation) statt einer langen
  flachen Liste; deutsche und englische Feldbeschriftungen/-beschreibungen
  über `translations/`. **Achtung:** bestehende Werte müssen nach dem Update
  einmalig neu eingetragen werden (siehe Abschnitt 6).
- **1.2.1** — Die Synology-Kachel zeigt bei Verbindungs-/Login-Problemen jetzt den
  tatsächlichen Grund an (z. B. Verbindungsfehler zu Host/Port, oder den
  konkreten DSM-Fehlercode samt Klartext wie „Zugriff verweigert" oder
  „Quell-IP blockiert") statt einer generischen Meldung – hilfreich zur
  Fehlersuche bei Netzwerk-/Firewall-/Berechtigungsproblemen.
- **1.2.0** — Neue Synology-DiskStation-Kachel: CPU/RAM/Netzwerk/Temperatur/Volumes,
  Top-CPU-Prozesse, angemeldete Benutzer, plus ein alle 6 h laufender
  Hintergrund-Scan der größten Ordner je Freigabe (2 Ebenen tief, ähnlich
  TreeSize). Konfiguration über `synology_*`-Add-on-Optionen, Login über die
  DSM-Web-API inkl. optionalem Geräte-Token für erzwungenes 2FA.
- **1.1.1** — Webseiten-Status-Kachel (Online/Offline, HTTP-Code, Antwortzeit) über
  `website.url`/`website.name`.
- **1.1.0** — Pi-hole-Kachel zeigt zusätzlich die Hardware des Pi-hole-Raspi
  (CPU/RAM/Storage/Temp/Uptime, aus dem dort laufenden Glances); Ubuntu-Kachel um
  Kernel, Prozesse, Swap und weitere Volumes erweitert; „Gesundheit" umbenannt in
  „unavailable/unknown entities"; Refresh-Button. Claude-Usage auf langlebigen
  `setup-token` umgestellt.
- **1.0.1** — HASS-Pi-Storage korrekt in GB statt Bytes; Dokumentation an reale
  Umgebung angepasst (venv/pipx-Glances, Login-Token-Exporter, sprachabhängige
  Systemmonitor-IDs, deutsche Menüpfade).
- **1.0.0** — Erstveröffentlichung.
