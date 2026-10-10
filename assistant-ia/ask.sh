#!/bin/bash
QUESTION="$*"
if [ -z "$QUESTION" ]; then
  echo "Usage : ./ask.sh \"votre question\""
  exit 1
fi

echo "❓ $QUESTION"
echo "─────────────────────────────────────────────────"

curl -s -X POST http://localhost:5000/chat \
  -H "Content-Type: application/json" \
  -d "{\"question\": \"$QUESTION\"}" \
  | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
except Exception:
    print('⚠️  Réponse illisible (Flask lancé ?)')
    sys.exit(0)
if d.get('decision') == 'refus_hors_sujet':
    print('🚫', d.get('reponse', ''))
elif 'reponse' in d:
    print(d['reponse'].strip())
else:
    print('⚠️  Erreur :', d.get('erreur') or d.get('erreur_groq')
          or d.get('erreur_rag') or d.get('erreur_routage') or d)
"
echo
