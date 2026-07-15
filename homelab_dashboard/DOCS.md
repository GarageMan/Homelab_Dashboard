# Homelab Dashboard – Installation

Ein Home-Assistant-Add-on (laeuft auf dem HAOS-Pi, `192.168.1.7`), das die drei
Server auf einen Blick zeigt: **HASS-Pi**, **Ubuntu-Server** (`.75`) und **Pi-hole** (`.5`),
plus eine **Claude-Usage**-Kachel.

Datenquellen:
- HASS-Pi: Supervisor- + Core-API (im Add-on ohne Zusatzkonfiguration verfuegbar)
- Ubuntu-Server & Pi-hole: **Glances** (REST-API, Port 61208)
- Pi-hole zusaetzlich: **Pi-hole-v6-API**
- Claude-Usage: **Exporter** auf dem Ubuntu-Server (Port 8787)

---

## 1. Glances auf Ubuntu-Server und Pi-hole

Auf **beiden** Linux-Servern (`192.168.1.75` und `192.168.1.5`):

```bash
sudo apt update
sudo apt install -y pipx lm-sensors
pipx ensurepath
pipx install 'glances[web]'
sudo sensors-detect --auto        # fuer CPU-Temperatur (auf dem Pi optional)

# Web/REST-API-Modus als Dienst
sudo cp glances-web.service /etc/systemd/system/glances-web.service
# Pfad zu glances pruefen und ggf. in der .service-Datei anpassen:
which glances                     # z. B. /home/<user>/.local/bin/glances
sudo systemctl daemon-reload
sudo systemctl enable --now glances-web.service

# Test:
curl http://localhost:61208/api/4/cpu
```

> Sicherheit: Glances bindet an alle Interfaces und ist ohne Auth im LAN lesbar.
> In einem vertrauenswuerdigen Heimnetz ok. Sonst mit `--password` starten
> (in der `.service`-Datei ergaenzen) oder per Firewall auf die HASS-Pi-IP begrenzen:
> `sudo ufw allow from 192.168.1.7 to any port 61208 proto tcp`

---

## 2. Claude-Usage-Exporter (nur Ubuntu-Server, .75)

### 2a. Claude Code einmalig anmelden
Der Exporter braucht ein Claude-Code-OAuth-Token. Du nutzt Claude Code nie zum
Coden – der Login dient nur als sich selbst erneuernde Anmeldung:

```bash
# Claude Code installieren (falls noch nicht vorhanden) und anmelden:
#   siehe https://docs.claude.com  ->  Claude Code
claude            # dann /login  -> Browser-Code-Flow mit deinem Claude-Abo
```

Danach existiert `~/.claude/.credentials.json`. Die Usage-Zahlen sind identisch
zu deiner Windows-Desktop-App (Limits gelten kontoweit).

### 2b. Exporter als Dienst
```bash
sudo mkdir -p /opt/claude-usage
sudo cp claude-usage-exporter.py /opt/claude-usage/
sudo chmod +x /opt/claude-usage/claude-usage-exporter.py

# WICHTIG: <user> = der Benutzer, der bei Claude Code angemeldet ist
sudo cp claude-usage-exporter.service /etc/systemd/system/claude-usage-exporter@.service
sudo systemctl daemon-reload
sudo systemctl enable --now claude-usage-exporter@<user>.service

# Test:
curl http://localhost:8787/usage
```

### 2c. Token frisch halten (optional, empfohlen)
Da du Claude Code sonst nicht benutzt, kann das Token irgendwann ablaufen. Ein
taeglicher Mini-Aufruf laesst Claude Code die Credentials erneuern:

```bash
( crontab -l 2>/dev/null; echo "17 4 * * * /usr/bin/claude -p 'ping' >/dev/null 2>&1" ) | crontab -
```

Falls `/usage` einmal `HTTP 401` meldet: einmal `claude` ausfuehren – erledigt.

---

## 3. HASS-Pi: Systemmonitor aktivieren (fuer CPU/Temp/RAM des Pi)

In Home Assistant: **Einstellungen -> Geraete & Dienste -> Integration hinzufuegen
-> „Systemmonitor"**. Das legt u. a. `sensor.processor_use`,
`sensor.processor_temperature`, `sensor.memory_use_percent` an – genau die Werte,
die das Dashboard fuer die HASS-Pi-Kachel liest. (Ohne die Integration zeigt die
Kachel weiterhin Version/Updates/Entitaeten, nur die Live-Balken bleiben leer.)

---

## 4. Das Add-on installieren

### Variante A – als Repository per URL (empfohlen)
1. In HA: **Einstellungen -> Add-ons -> Add-on-Store -> ⋮ -> Repositories**
2. URL des Repos eintragen (`https://github.com/<user>/homelab-dashboard`) -> **Hinzufuegen**
   (Repo muss oeffentlich sein – es enthaelt keine Geheimnisse.)
3. Store neu laden -> „Homelab Dashboard" erscheint -> **Installieren**
   (der Pi baut das Image, dauert 1–2 Minuten)

### Variante B – lokal (ohne GitHub)
Ueber dein **Samba**- oder **SSH**-Add-on den Ordner `homelab_dashboard/` ablegen unter:
```
\\192.168.1.7\addons\homelab_dashboard\
```
Dann **Add-on-Store -> ⋮ -> Neu laden** -> unter **Lokale Add-ons** installieren.

### Danach (beide Varianten)
Tab **Konfiguration**: Werte pruefen/setzen
   - `ubuntu_host: 192.168.1.75`
   - `pihole_host: 192.168.1.5`
   - `glances_port: 61208`
   - `pihole_password:` dein Pi-hole-**App-Passwort**
     (Pi-hole-Weboberflaeche -> Settings -> Web interface / API -> App password)
   - `usage_url: http://192.168.1.75:8787/usage`
   - `refresh_seconds: 15`
4. **Starten**, „Auf Seitenleiste anzeigen" aktivieren -> Dashboard oeffnet sich
   direkt in Home Assistant (Ingress, mit HA-Login, kein offener Port).

---

## Fehlersuche

| Symptom | Ursache / Loesung |
|---|---|
| Add-on baut nicht | Logs im Add-on-Tab ansehen; Internet fuer den `pip install`-Schritt noetig |
| Ubuntu/Pi-hole „nicht erreichbar" | `curl http://<ip>:61208/api/4/cpu` vom Pi aus testen; Firewall/Dienst pruefen |
| Pi-hole „auth fehlgeschlagen" | App-Passwort falsch/leer; in den Add-on-Optionen korrigieren |
| HASS-Kachel ohne CPU/Temp | Systemmonitor-Integration aktivieren (Schritt 3) |
| Claude Usage „nicht erreichbar" | Exporter-Dienst laeuft? `systemctl status claude-usage-exporter@<user>` |
| Claude Usage `HTTP 401` | Token abgelaufen -> einmal `claude` ausfuehren; Keep-alive-Cron setzen |

Alle Werte lassen sich spaeter im Add-on-Tab **Konfiguration** aendern – kein Rebuild noetig.
