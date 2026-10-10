#!/bin/bash
# Redémarre Flask natif WSL
# Appelé par l'action Zabbix quand /health/* échoue

logger -t zbx-flask-remediation "Tentative de redémarrage de Flask"

# 1. Tuer l'instance existante (si elle tourne)
pkill -f "python3 app.py" 2>/dev/null
sleep 2

# 2. Vérifier qu'il n'y a plus rien sur le port 5000
if ss -tlnp 2>/dev/null | grep -q ":5000"; then
    logger -t zbx-flask-remediation "ECHEC: port 5000 encore occupé après pkill"
    exit 1
fi

# 3. Relancer Flask en arrière-plan
cd /home/itach/assistant-ia || exit 1
source venv/bin/activate

nohup python3 app.py > /tmp/flask.log 2>&1 &
FLASK_PID=$!
echo $FLASK_PID > /tmp/flask.pid

# 4. Attendre que Flask réponde (max 30s, préchauffage bge-m3 inclus)
for i in {1..30}; do
    if curl -sf http://localhost:5000/health/db > /dev/null 2>&1; then
        logger -t zbx-flask-remediation "SUCCES: Flask redémarré (PID $FLASK_PID) en ${i}s"
        exit 0
    fi
    sleep 1
done

logger -t zbx-flask-remediation "ECHEC: Flask ne répond pas après 30s"
exit 1
