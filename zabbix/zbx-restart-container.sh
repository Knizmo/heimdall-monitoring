#!/bin/bash
# Auto-remediation : redemarre un conteneur Docker arrete
# Usage: zbx-restart-container.sh "nom_du_trigger"
# Le script extrait le nom du conteneur du texte du trigger

TRIGGER_NAME="$1"

# Extraction : cherche un mot commence par "/" suivi de caracteres (ex: /mon-nginx)
CONTAINER=$(echo "$TRIGGER_NAME" | grep -oP '/[a-zA-Z0-9_-]+' | head -1 | tr -d '/')

if [ -z "$CONTAINER" ]; then
    logger -t zbx-remediation "ERREUR: nom de conteneur introuvable dans: $TRIGGER_NAME"
    echo "ERREUR: nom de conteneur introuvable dans: $TRIGGER_NAME"
    exit 1
fi

logger -t zbx-remediation "Tentative de redemarrage: $CONTAINER"
RESULT=$(docker start "$CONTAINER" 2>&1)
EXIT_CODE=$?

if [ $EXIT_CODE -eq 0 ]; then
    logger -t zbx-remediation "SUCCES: $CONTAINER redemarre"
    echo "SUCCES: $CONTAINER redemarre"
else
    logger -t zbx-remediation "ECHEC: $CONTAINER - $RESULT"
    echo "ECHEC: $CONTAINER - $RESULT"
fi

exit $EXIT_CODE
