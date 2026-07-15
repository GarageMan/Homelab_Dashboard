[Unit]
Description=Glances (Web/REST-API-Modus) fuer Homelab Dashboard
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
# -w = Web/REST-API (Port 61208). Bindet standardmaessig an alle Interfaces.
# Nur im vertrauenswuerdigen LAN betreiben ODER mit --password absichern.
ExecStart=/usr/local/bin/glances -w -t 5
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
