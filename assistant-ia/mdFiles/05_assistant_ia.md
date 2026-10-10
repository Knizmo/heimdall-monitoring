# Assistant IA — Couche 4 : Heimdall

## Vue d'ensemble de l'assistant

L'assistant s'appelle **Heimdall**. C'est un assistant technique expert en supervision d'infrastructure, DevOps et ingénierie système. Il répond en langage naturel aux questions sur l'état de l'infrastructure, les procédures de supervision, et les concepts techniques liés au projet.

Heimdall combine trois sources à chaque question : une recherche dans la documentation du projet (RAG), une lecture live de l'API Zabbix (état réel des hôtes, problèmes actifs, métriques), et une génération de réponse par un grand modèle de langage via Groq.

## Architecture technique

Le pipeline complet pour chaque question est le suivant :

```
Question utilisateur (POST /chat)
   ▼
Flask (app.py, port 5000)
   ├─► bge-m3 (Ollama local) → embedding 1024D → pgvector → top-5 chunks doc
   ├─► API Zabbix → hôtes + problèmes actifs + métriques live
   └─► Groq API (openai/gpt-oss-120b) → réponse structurée en markdown
```

## Choix du modèle de génération — Groq

L'assistant utilise **Groq** (plateforme d'inférence rapide sur LPU) avec le modèle **openai/gpt-oss-120b**. Ce choix a été fait après avoir testé llama3.2:3b en local via Ollama, qui présentait trois problèmes : lenteur (~10s par réponse), hallucinations fréquentes, et non-respect des règles du prompt.

**Historique du choix de modèle** : le projet utilisait initialement llama-3.3-70b-versatile (Meta, 70B, architecture dense) sur Groq. Ce modèle a été déprécié par Groq le 17 juin 2026 et retiré définitivement le 16 août 2026. La migration vers openai/gpt-oss-120b (modèle open-weight d'OpenAI, architecture Mixture-of-Experts, 120 milliards de paramètres au total dont seule une fraction est activée par token) a été faite en changeant uniquement la variable CHAT_MODEL dans le fichier .env, sans toucher au code de app.py — ce qui valide le découplage entre l'architecture du projet et le choix d'un modèle précis.

Groq résout les trois problèmes simultanément : temps de réponse ~1s, modèle qui suit les consignes strictes grâce à son architecture Mixture-of-Experts optimisée pour le raisonnement, et bon support du français. Le free tier Groq est suffisant pour un usage de supervision (30 requêtes/minute).

L'URL de l'API Groq est compatible OpenAI : `https://api.groq.com/openai/v1/chat/completions`. La clé API commence par `gsk_` et est stockée dans `.env` sous `GROQ_API_KEY`.

## Choix des embeddings — bge-m3 local

Les embeddings sont générés localement par **bge-m3** via Ollama (dimension 1024). Ce modèle reste local pour deux raisons : il est gratuit (pas de coût par appel), et il est excellent en multilingue (français inclus). Il utilise ~1.2 Go de VRAM sur le GPU GTX 1660 Ti.

Au démarrage de Flask, un embedding de préchauffage est lancé automatiquement pour charger bge-m3 en VRAM et éviter le cold start de ~9s sur la première question.

## Stockage vectoriel — pgvector

Les vecteurs des chunks de documentation sont stockés dans la base `assistant_ia` de `mon-postgres`, table `documentation(id, contenu, source, embedding vector(1024), cree_le)`. L'extension pgvector 0.8.4 est installée dans le conteneur.

La recherche sémantique utilise l'opérateur `<=>` (distance cosinus) : `SELECT source, contenu FROM documentation ORDER BY embedding <=> vecteur LIMIT 5`. Les 5 chunks les plus proches sont injectés dans le prompt envoyé à Groq.

Pas d'index HNSW créé volontairement : le volume est petit (quelques dizaines de chunks) et la RAM WSL est contrainte (3,8 Go).

## Données live Zabbix injectées dans le prompt

À chaque question, l'assistant récupère trois vues de l'état actuel via l'API JSON-RPC de Zabbix (token Bearer, URL `http://localhost:8080/api_jsonrpc.php`) :

**Hôtes** (`host.get`) : liste exacte des hôtes supervisés et leur statut. Depuis Zabbix 6.0+, le champ `available` de host.get est peu fiable (disponibilité passée au niveau interface). L'assistant s'appuie sur les problèmes actifs pour l'état réel.

**Problèmes actifs** (`problem.get`) : triggers actuellement en PROBLEM, triés par eventid (tri par severity+clock rejeté depuis Zabbix 7.0 — erreur `Invalid params`). Si la liste est vide, rien n'est en panne.

**Métriques live** (`item.get` avec `lastvalue`) : dernière valeur mesurée de chaque item clé, formatée de façon lisible :

| Clé Zabbix | Formatage |
|---|---|
| `docker.container_stats.memory.usage_total` | Converti en Mo/Go |
| `docker.container_stats.cpu_pct_usage` | Affiché en `x.xx %` |
| `docker.networks.rx_bytes` | Converti en Ko/s ou Mo/s |
| `docker.networks.tx_bytes` | Converti en Ko/s ou Mo/s |
| `docker.container_info.state.running` | `en marche` ou `arrêté` |
| `pgsql.custom.*` | Valeur brute + unité |

Chaque vue est isolée dans un try/except indépendant : si `problem.get` échoue, `item.get` passe quand même. L'assistant reste partiellement fonctionnel même si Zabbix est temporairement injoignable.

## Format du prompt envoyé à Groq

Le prompt est structuré en XML pour que le modèle distingue clairement les sources :

```
<hotes nb="N">
  <hote nom="Zabbix server" supervision="activé" />
</hotes>

<problemes nb="0"><!-- aucun problème actif --></problemes>

<metriques nb="N">
  <metrique nom="Container /mon-nginx: Memory usage total">14.5 Mo</metrique>
  <metrique nom="Container /mon-nginx: CPU percent usage">0.02 %</metrique>
  ...
</metriques>

<documentation>
  <passage id="1" source="02_supervision_zabbix.md § Triggers prototypes Docker">
    ...contenu du chunk...
  </passage>
  ...
</documentation>

<question>la question de l'utilisateur</question>
```

## Règles du prompt Heimdall

Règles non négociables sur les données live :
1. Pour l'état actuel, s'appuyer UNIQUEMENT sur `<hotes>`, `<problemes>`, `<metriques>`.
2. Ne jamais inventer de nom d'hôte — la liste réelle est exactement celle de `<hotes>`.
3. Si un hôte cité est absent de `<hotes>`, le dire et lister les hôtes présents.
4. Si `<problemes nb="0">`, répondre : « Aucun problème actif actuellement. »
5. Un trigger dans `<documentation>` est une définition, pas un événement en cours.
6. Ne jamais recopier les balises XML dans la réponse.

Liberté accordée :
- Analyser et interpréter les métriques (signaler une anomalie même sans trigger actif).
- Proposer des pistes de diagnostic et des recommandations techniques.
- Expliquer des concepts techniques librement (Zabbix, Docker, RAG, LLM...).
- Utiliser du markdown pour structurer les rapports (titres ##, listes, **gras**).
- Avoir de la personnalité — Heimdall est le gardien de l'infrastructure.

Périmètre accepté : supervision/monitoring, Docker, PostgreSQL, Python/Flask, DevOps, Linux, Nginx, IA/RAG, ingénierie en général. Hors périmètre : tout ce qui n'a aucun rapport avec la technique.

## Ingestion de la documentation (RAG)

Le script `ingest.py` découpe chaque fichier `.md` par sections H1/H2/H3, génère un embedding bge-m3 par section, et insère dans la table `documentation`. Le processus est idempotent : il supprime les anciens chunks du même fichier avant d'insérer les nouveaux.

Les 5 fichiers sources du RAG sont :
- `01_infrastructure.md` — socle Docker, WSL, bases de données
- `02_supervision_zabbix.md` — templates, items, triggers, LLD, UserParameters, API
- `03_alertes_remediation.md` — Discord, Email, auto-remédiation, script, action Zabbix
- `04_procedures.md` — commandes courantes, tests, debug
- `05_assistant_ia.md` — ce fichier, architecture Couche 4

Commande :
```bash
cd ~/assistant-ia && source venv/bin/activate
python3 ingest.py 01_infrastructure.md 02_supervision_zabbix.md \
  03_alertes_remediation.md 04_procedures.md 05_assistant_ia.md
```

Vérification : `curl -s http://localhost:5000/health/db | python3 -m json.tool` — le champ `chunks_documentation` doit être supérieur à 0.

## Endpoints de l'assistant

```bash
# Poser une question
curl -s -X POST http://localhost:5000/chat \
  -H "Content-Type: application/json" \
  -d '{"question": "quel conteneur consomme le plus de mémoire ?"}' \
  | python3 -m json.tool

# Voir les métriques Zabbix brutes (debug)
curl -s http://localhost:5000/debug/metriques | python3 -m json.tool

# Healthchecks
curl -s http://localhost:5000/health/groq   | python3 -m json.tool
curl -s http://localhost:5000/health/db     | python3 -m json.tool
curl -s http://localhost:5000/health/ollama | python3 -m json.tool
curl -s http://localhost:5000/health/zabbix | python3 -m json.tool
```

## Helper CLI ask.sh

```bash
chmod +x ~/assistant-ia/ask.sh
echo 'alias ask="~/assistant-ia/ask.sh"' >> ~/.bashrc && source ~/.bashrc

ask "donne moi un rapport complet de l'infrastructure"
ask "y a-t-il des anomalies dans les métriques actuelles ?"
ask "explique moi la différence entre un trigger et un problème dans Zabbix"
ask "si mon-nginx tombe, qu'est-ce qui se passe automatiquement ?"
ask "pourquoi utilise-t-on pgvector plutôt qu'une base classique ?"
```

## Historique des échanges

Chaque question et réponse est sauvegardée dans la table `historique_chat` de la base `assistant_ia` :

```bash
docker exec -it mon-postgres psql -U postgres -d assistant_ia \
  -c "SELECT id, left(question,50), left(reponse,60), cree_le \
      FROM historique_chat ORDER BY id DESC LIMIT 10;"
```
