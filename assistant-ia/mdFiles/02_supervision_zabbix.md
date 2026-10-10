# Supervision Zabbix : templates, items, triggers

## Découverte automatique des conteneurs Docker (LLD)

Le template appliqué à l'hôte "Zabbix server" est **"Docker by Zabbix agent 2"**. Il fournit une règle de découverte qui détecte automatiquement tous les conteneurs Docker actifs et leur applique des items et triggers prototypes.

Prérequis : l'utilisateur système `zabbix` doit être dans le groupe `docker` (`sudo usermod -aG docker zabbix`). Test : `sudo -u zabbix zabbix_agent2 -t docker.info`.

La macro de découverte s'appelle `{#NAME}` (attention, pas `{#CONTAINER.NAME}`). La règle de découverte est "Containers discovery", clé `docker.containers.discovery[false]`. Le `[false]` signifie que seuls les conteneurs actifs sont retournés — cette clé est verrouillée par le template.

## Réglages critiques de la règle de découverte

- **Update interval : 1m** — détection rapide (au lieu de 1h par défaut).
- **Disable lost resources : Never** — CRITIQUE : empêche la désactivation des triggers quand un conteneur arrêté disparaît de la découverte.
- **Delete lost resources : After 1h** — nettoie les données des conteneurs supprimés après 1h.

Leçon apprise : quand un conteneur s'arrête, il disparaît de la découverte (clé `[false]`). Avec "Disable lost resources = Never", le trigger reste actif et peut passer en PROBLEM. C'est ce mécanisme qui fait fonctionner la détection d'arrêt.

## Triggers prototypes Docker

Tous ces triggers utilisent `{#NAME}` pour s'appliquer automatiquement à tous les conteneurs, présents et futurs :

- **"Erreur dans le conteneur"** — fourni par le template, sévérité Warning.
- **"Arrêté avec code d'erreur"** — expression : `last(state.exitcode["{#NAME}"]) > 0 et last(state.running["{#NAME}"]) = 0`. Sévérité Average. `docker stop` (exit 0) ne le déclenche PAS ; utiliser `docker kill` (exit 137) pour tester.
- **"Santé unhealthy"** — expression : `count(state.health["{#NAME}"], 2m,, 2) >= 2`. Sévérité High.
- **"N'est plus actif"** (custom) — expression : `last(docker.container_info.state.running["{#NAME}"]) = 0`. Sévérité High. Se déclenche sur TOUT arrêt, propre ou brutal. C'est celui qui déclenche l'auto-remédiation.
- **"Mémoire > 55 Mo"** (custom) — expression : `last(docker.container_stats.memory.usage_total["{#NAME}"]) > 55M`. Sévérité Warning.
- **"Trafic réseau détecté"** (custom) — expression : `last(docker.networks.rx_bytes["{#NAME}"]) > 10K`. Sévérité Information.

## Clés d'items Docker utilisées

Ces clés remontent les métriques que l'assistant IA lit pour répondre aux questions sur l'état actuel :

- `docker.container_stats.memory.usage_total["nom"]` — mémoire consommée en octets.
- `docker.container_info.state.running["nom"]` — 1 si en marche, 0 si arrêté.
- `docker.container_info.state.health["nom"]` — état de santé.
- `docker.container_info.state.exitcode["nom"]` — code de sortie du dernier arrêt.
- `docker.networks.rx_bytes["nom"]` — octets reçus.

## Supervision PostgreSQL — Template officiel

Le template **"PostgreSQL by Zabbix agent 2"** est lié à mon-postgres. Il nécessite un utilisateur de supervision dédié :

```sql
CREATE USER zbx_monitor WITH PASSWORD 'ZbxMonitor123!' INHERIT;
GRANT pg_monitor TO zbx_monitor;
```

Les macros de connexion à définir sur l'hôte :

| Macro | Valeur |
|---|---|
| `{$PG.URI}` | `tcp://localhost:5433` |
| `{$PG.USER}` | `zbx_monitor` |
| `{$PG.PASSWORD}` | `ZbxMonitor123!` |
| `{$PG.DATABASE}` | `postgres` |

Items utiles : Database size, Commits/sec, Tuples inserted/updated/returned per second, Backends connected, Cache hit ratio, Deadlocks, Num of locks total.

Attention : les items "per second" sont des TAUX. Une insertion trop brève entre deux collectes (1 minute) peut passer inaperçue.

## UserParameters personnalisés pour PostgreSQL

Fichier `/etc/zabbix/zabbix_agent2.d/postgres_custom.conf` :

```conf
UserParameter=pgsql.custom.tables_count,docker exec mon-postgres psql -U zbx_monitor -d postgres -tAc "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='public';"
UserParameter=pgsql.custom.games_count,docker exec mon-postgres psql -U zbx_monitor -d postgres -tAc "SELECT COUNT(*) FROM games;"
UserParameter=pgsql.custom.studios_count,docker exec mon-postgres psql -U zbx_monitor -d postgres -tAc "SELECT COUNT(*) FROM studios;"
UserParameter=pgsql.custom.personnages_count,docker exec mon-postgres psql -U zbx_monitor -d postgres -tAc "SELECT COUNT(*) FROM personnages;"
UserParameter=pgsql.custom.plateformes_count,docker exec mon-postgres psql -U zbx_monitor -d postgres -tAc "SELECT COUNT(*) FROM plateformes;"
```

Droits nécessaires : `GRANT SELECT ON ALL TABLES IN SCHEMA public TO zbx_monitor;` + `ALTER DEFAULT PRIVILEGES...`.

Après modification : `sudo systemctl restart zabbix-agent2`. Test : `sudo -u zabbix zabbix_agent2 -t pgsql.custom.tables_count`.

## API JSON-RPC de Zabbix

L'assistant IA interroge l'API Zabbix pour lire l'état live. URL : `http://localhost:8080/api_jsonrpc.php`. Authentification par en-tête `Authorization: Bearer <token>`. Le token nommé `assistant-ia` a été créé dans Users → API tokens (user Admin, sans expiration).

Méthodes utilisées par l'assistant :

- `host.get` avec `output=[host, status]` — liste des hôtes supervisés et leur statut (0=activé, 1=désactivé).
- `problem.get` avec `recent=False` et tri par sévérité — liste les triggers actuellement en PROBLEM (encore ouverts).
- `item.get` avec `output=[name, key_, lastvalue, units]` et `search` sur les clés du projet — récupère la dernière valeur mesurée de chaque item (mémoire, état, compteurs).

Important : depuis Zabbix 6.0+, le champ `available` de host.get est peu fiable — la disponibilité est passée au niveau interface. L'assistant s'appuie donc sur les problèmes actifs (problem.get) pour l'état réel, pas sur `available`.
