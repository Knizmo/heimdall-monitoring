# Procédures et commandes courantes

## Démarrer une session WSL

À l'ouverture de WSL, tout est arrêté. Pour redémarrer la stack :

```bash
sudo service postgresql start
sudo systemctl restart zabbix-server zabbix-agent2 nginx php8.3-fpm
~/start-dockers.sh
```

Puis vérifier l'accès Zabbix sur http://localhost:8080.

## Gérer les conteneurs Docker

```bash
docker ps                     # conteneurs actifs
docker ps -a                  # tous les conteneurs (même arrêtés)
docker stop mon-nginx         # arrêt propre (garde l'ID)
docker start mon-nginx        # relance (garde l'ID)
docker kill mon-nginx         # arrêt brutal (exit 137, teste trigger erreur)
docker stats mon-postgres --no-stream   # mémoire réelle actuelle
```

RÈGLE ABSOLUE : jamais `docker rm` sur les conteneurs supervisés — le changement d'ID casse la découverte Zabbix, et détruirait pgvector installé dans mon-postgres.

## Accéder à PostgreSQL

Base "postgres" (données jeux vidéo) :
```bash
docker exec -it mon-postgres psql -U postgres
```

Base "assistant_ia" (documentation RAG et historique du chat IA) :
```bash
docker exec -it mon-postgres psql -U postgres -d assistant_ia
```

Commandes utiles dans psql :
- `\dt` : lister les tables.
- `\dx` : lister les extensions installées (dont pgvector).
- `SELECT * FROM games;` : voir le contenu d'une table.
- `SELECT COUNT(*) FROM documentation;` : compter les chunks RAG.

## Tester un item Zabbix agent

```bash
sudo -u zabbix zabbix_agent2 -t docker.info
sudo -u zabbix zabbix_agent2 -t docker.containers.discovery
sudo -u zabbix zabbix_agent2 -t pgsql.custom.tables_count
sudo -u zabbix zabbix_agent2 -t pgsql.custom.games_count
```

Si un test renvoie une valeur inattendue, vérifier :
1. Que l'agent est bien redémarré après modification du fichier de conf.
2. Que l'utilisateur `zbx_monitor` a les droits `SELECT` sur les tables (pour les UserParameters PostgreSQL).
3. Que le fichier `/etc/zabbix/zabbix_agent2.d/postgres_custom.conf` contient bien la clé attendue.

## Tester l'auto-remédiation manuellement

Simulation d'un conteneur qui tombe :

```bash
docker stop mon-nginx
# attendre 1 à 2 minutes : le trigger passe en PROBLEM,
# le script se lance, mon-nginx redémarre, notif reçue
```

Test manuel du script sans passer par Zabbix :

```bash
sudo -u zabbix /usr/local/bin/zbx-restart-container.sh "Docker: Container /mon-nginx n'est plus actif"
```

Suivi des logs pendant le test :

```bash
sudo tail -f /var/log/syslog | grep zbx-remediation
sudo tail -f /var/log/zabbix/zabbix_server.log | grep -iE "action|command|script"
```

## Importer des données jeux vidéo dans PostgreSQL

Script Python interactif dans `~/json-api/` :

```bash
cd ~/json-api && python3 import_data.py
```

Menu coloré : choix de la collection (games, studios, personnages, plateformes), puis toute la collection [T], une ligne [id], retour [R] ou quitter [Q]. Anti-doublons (`ON CONFLICT (id) DO UPDATE`). Boucle jusqu'à Q.

## Tester l'API Zabbix directement

```bash
export TOKEN="<ton_jeton_zabbix>"
curl -s -X POST http://localhost:8080/api_jsonrpc.php \
  -H "Content-Type: application/json-rpc" \
  -H "Authorization: Bearer $TOKEN" \
  -d '{"jsonrpc":"2.0","method":"host.get","params":{"output":["host","status"]},"id":1}'
```

Pour lister les problèmes actifs :

```bash
curl -s -X POST http://localhost:8080/api_jsonrpc.php \
  -H "Content-Type: application/json-rpc" \
  -H "Authorization: Bearer $TOKEN" \
  -d '{"jsonrpc":"2.0","method":"problem.get","params":{"output":["name","severity"],"recent":false},"id":1}'
```

## Générer du trafic (tester le trigger réseau)

```bash
for i in {1..3000}; do curl -s http://localhost:8083/games > /dev/null; done
```

## Utiliser l'assistant IA

Démarrer Flask (en dev, hot-reload activé) :

```bash
cd ~/assistant-ia && source venv/bin/activate
python3 app.py
```

Vérifier les 4 healthchecks :

```bash
curl -s http://localhost:5000/health/db     | python3 -m json.tool
curl -s http://localhost:5000/health/ollama | python3 -m json.tool
curl -s http://localhost:5000/health/groq   | python3 -m json.tool
curl -s http://localhost:5000/health/zabbix | python3 -m json.tool
```

Poser une question via le helper CLI :

```bash
ask "combien de mémoire utilise mon-nginx ?"
ask "quels sont les problèmes actifs ?"
ask "comment fonctionne l'auto-remédiation ?"
```

## (Ré)ingérer la documentation dans le RAG

Après modification d'un `.md`, relancer l'ingestion (idempotent) :

```bash
cd ~/assistant-ia && source venv/bin/activate
python3 ingest.py 01_infrastructure.md 02_supervision_zabbix.md 03_alertes_remediation.md 04_procedures.md
```

Le script supprime automatiquement les anciens chunks du même fichier avant d'insérer les nouveaux. Vérification :

```bash
curl -s http://localhost:5000/health/db | python3 -m json.tool
# doit afficher chunks_documentation > 0
```

## Consulter l'historique du chat

```bash
docker exec -it mon-postgres psql -U postgres -d assistant_ia \
  -c "SELECT id, left(question,50), left(reponse,60), cree_le \
      FROM historique_chat ORDER BY id DESC LIMIT 10;"
```
