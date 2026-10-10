#!/bin/bash
# Arrête proprement toute la session Heimdall / infrastructure Zabbix
# Ordre important : Zabbix en premier pour désactiver toute évaluation
# de trigger avant de toucher à Flask ou aux conteneurs Docker.

echo "→ Arrêt de la supervision Zabbix (évite toute auto-remédiation pendant le shutdown)..."
sudo systemctl stop zabbix-server zabbix-agent2

echo "→ Arrêt de Flask..."
pkill -f "python3 app.py" 2>/dev/null
rm -f /tmp/flask.pid

echo "→ Arrêt des conteneurs Docker..."
docker stop mon-nginx mon-postgres mon-json-api

echo "→ Arrêt des services système restants..."
sudo systemctl stop nginx php8.3-fpm
sudo service postgresql stop

echo "✓ Session arrêtée proprement, sans risque d'auto-remédiation."
