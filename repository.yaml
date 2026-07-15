[Unit]
Description=Claude Usage Exporter (fuer Homelab Dashboard)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
# WICHTIG: User, der bei Claude Code angemeldet ist (dessen ~/.claude/.credentials.json genutzt wird)
User=%i
ExecStart=/usr/bin/python3 /opt/claude-usage/claude-usage-exporter.py
Environment=USAGE_PORT=8787
Environment=USAGE_TTL=90
Restart=on-failure
RestartSec=10
# Haertung
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=read-only
PrivateTmp=true

[Install]
WantedBy=multi-user.target
