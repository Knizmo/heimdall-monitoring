# Interface web et reverse proxy

## Vue d'ensemble

L'assistant Heimdall est désormais accessible via une interface web, en plus du helper CLI `ask.sh`. Le trafic passe par le conteneur Docker `mon-nginx`, qui agit comme reverse proxy vers Flask.

```
Navigateur
   ▼
mon-nginx (Docker, port 8081 → 80 interne)   ← reverse proxy
   ▼
Flask (natif WSL, port 5000)
   ▼
bge-m3 + pgvector + API Zabbix + Groq
```

## Interface web du chatbot

Page unique HTML/CSS/JavaScript, servie directement par Flask via la route racine `/`. Fichier situé dans `~/assistant-ia/static/index.html`. Trois zones fonctionnelles cohabitent dans cette interface : le chat, la supervision live, et le CRUD des bases de données.

### Zone chat (centrale)

- Zone de saisie de question et bouton d'envoi
- Affichage des réponses avec rendu markdown (titres, listes, gras)
- Historique de conversation affiché dans la page
- Indicateur de chargement pendant la génération Groq

### Zone supervision (dédiée)

Une zone de l'interface est réservée à l'affichage en direct de l'état de l'infrastructure, indépendamment du chat :

- Problèmes actifs (issus de `problem.get`, sévérité incluse)
- État de santé des conteneurs (running/arrêté, health)
- Métriques live : mémoire, CPU, réseau par conteneur

Cette zone consomme l'endpoint `/debug/metriques` avec un rafraîchissement périodique côté JavaScript (polling), pour rester à jour sans que l'utilisateur ait besoin de recharger la page ou de poser une question au chat.

### Zone CRUD (gestion des bases de données)

Une troisième zone permet de gérer directement les deux bases de `mon-postgres` depuis l'interface web, sans passer par `psql` en ligne de commande :

- **Base `assistant_ia`** : CRUD sur `documentation` (les chunks RAG — utile pour corriger ou retirer un chunk sans re-ingérer tout le fichier source) et sur `historique_chat` (consultation, purge).
- **Base `postgres`** (jeux vidéo) : CRUD complet sur `games`, `studios`, `personnages`, `plateformes` — en complément du script `import_data.py` existant, qui reste la méthode privilégiée pour les imports en masse depuis l'API JSON.

Ces deux zones (supervision et CRUD) reposent sur des routes Flask dédiées, distinctes du pipeline `/chat` :

| Endpoint | Méthode | Rôle |
|---|---|---|
| `/debug/metriques` | GET | Alimente la zone supervision |
| `/api/assistant_ia/documentation` | GET/POST/PUT/DELETE | CRUD chunks RAG |
| `/api/assistant_ia/historique_chat` | GET/DELETE | Consultation/purge de l'historique |
| `/api/jeux/games` | GET/POST/PUT/DELETE | CRUD table games |
| `/api/jeux/studios` | GET/POST/PUT/DELETE | CRUD table studios |
| `/api/jeux/personnages` | GET/POST/PUT/DELETE | CRUD table personnages |
| `/api/jeux/plateformes` | GET/POST/PUT/DELETE | CRUD table plateformes |

Flask sert la page principale via une route dédiée :
```python
from flask import send_from_directory

@app.route("/")
def index():
    return send_from_directory("static", "index.html")
```

Accès direct sans passer par le reverse proxy : `http://localhost:5000`.

## Pourquoi un reverse proxy

Un reverse proxy est un intermédiaire côté serveur : le visiteur ne voit pas directement quel service répond à sa requête. Il tape une seule adresse (`localhost:8081`), et Nginx se charge en interne de rediriger vers Flask (`5000`). Cela diffère d'un proxy classique, qui protège le client plutôt que le serveur.

Le rôle de Nginx ici est uniquement l'acheminement (routing) — aucune logique métier ne s'y trouve. Toute la logique de l'assistant (RAG, Zabbix live, appel Groq) reste entièrement dans Flask.

## Pourquoi mon-nginx (conteneur Docker) plutôt que le Nginx système

Deux options étaient possibles : le Nginx installé au niveau système (qui sert déjà l'interface Zabbix sur le port 8080), ou le conteneur Docker `mon-nginx` (qui jusque-là ne servait qu'à être supervisé comme démonstration).

Le choix s'est porté sur le conteneur `mon-nginx`, pour une cohérence architecturale : tout le projet reste dockerisé, et `mon-nginx` cesse d'être un simple conteneur de démo pour rendre un vrai service.

Contrepartie assumée : l'interface web dépend désormais de la disponibilité de `mon-nginx`. Si ce conteneur est arrêté (par exemple pour tester l'auto-remédiation de la Couche 3), l'interface web du chatbot devient temporairement inaccessible pendant le redémarrage automatique (environ 1 à 2 minutes).

## Configuration du reverse proxy

Fichier de config situé dans `~/assistant-ia/nginx/heimdall.conf`, copié dans le conteneur via `docker cp` (pas de volume monté, pour éviter de recréer le conteneur et casser sa découverte Zabbix) :

```nginx
server {
    listen 80;
    server_name localhost;

    location / {
        proxy_pass http://172.17.0.1:5000;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;

        proxy_connect_timeout 60s;
        proxy_read_timeout    60s;
        proxy_send_timeout    60s;
    }
}
```

## L'adresse IP de la passerelle Docker

`172.17.0.1` est l'adresse de la passerelle réseau Docker : vue depuis l'intérieur d'un conteneur, c'est l'adresse qui pointe vers la machine hôte (le WSL). Sans cette IP, le conteneur `mon-nginx` n'aurait aucun moyen de savoir où trouver Flask, qui tourne en dehors de Docker.

Cette IP est récupérable dynamiquement :
```bash
docker inspect mon-nginx --format '{{range .NetworkSettings.Networks}}{{.Gateway}}{{end}}'
```

Elle est stable tant que le daemon Docker n'est pas recréé — un cas rare en usage normal. Elle est indépendante du réseau WiFi ou de la position géographique de la machine : changer de réseau WiFi ne casse pas le reverse proxy.

Note technique : l'image officielle `nginx` ne contient pas la commande `ip` (`sh: 1: ip: not found` si on tente `docker exec mon-nginx ip route`). Il faut donc récupérer l'IP de la passerelle depuis l'hôte WSL avec `docker inspect`, pas depuis l'intérieur du conteneur.

## Procédure d'application de la configuration

À exécuter à chaque modification du fichier `heimdall.conf` :

```bash
# 1. Récupérer l'IP de la passerelle (à vérifier si elle correspond à celle codée en dur dans le fichier)
GATEWAY_IP=$(docker inspect mon-nginx --format '{{range .NetworkSettings.Networks}}{{.Gateway}}{{end}}')
echo $GATEWAY_IP

# 2. Copier la config dans le conteneur (remplace la config par défaut de nginx)
docker cp ~/assistant-ia/nginx/heimdall.conf mon-nginx:/etc/nginx/conf.d/default.conf

# 3. Valider la syntaxe avant d'appliquer
docker exec mon-nginx nginx -t

# 4. Recharger Nginx sans redémarrer le conteneur (garde l'ID, la découverte Zabbix n'est pas affectée)
docker exec mon-nginx nginx -s reload
```

## Test du reverse proxy

```bash
# Test direct de Flask (sans passer par Nginx)
curl http://localhost:5000/

# Test via le reverse proxy
curl http://localhost:8081/
```

Si les deux commandes renvoient la même page, le reverse proxy fonctionne correctement.

## Cartographie complète des ports du projet

| Port | Service |
|---|---|
| 8081 (hôte) → 80 (interne) | mon-nginx (Docker) — reverse proxy vers Flask |
| 5000 | Flask (natif WSL) |
| 5433 → 5432 (interne) | mon-postgres (Docker) |
| 8083 | mon-json-api (Docker) |
| 8080 | Interface web Zabbix (Nginx système, distinct du conteneur mon-nginx) |
| 11434 | Ollama (bge-m3) |

Commandes utiles pour lister les ports en cas de doute :
```bash
docker ps --format "table {{.Names}}\t{{.Ports}}"   # ports de tous les conteneurs Docker
sudo ss -tlnp                                        # tous les ports en écoute sur WSL, Docker ou non
sudo lsof -i :5000                                   # qui utilise un port précis
```
