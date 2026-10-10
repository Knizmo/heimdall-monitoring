# Infrastructure du projet

## Vue d'ensemble

Le projet est une infrastructure Docker supervisée par Zabbix 7.0, tournant dans WSL2 (Ubuntu 24.04) sur une machine avec GPU NVIDIA GTX 1660 Ti. L'assistant IA local complète la supervision avec des réponses en langage naturel via Groq (openai/gpt-oss-120b) et un RAG (pgvector + bge-m3).

## Conteneurs Docker

Trois conteneurs sont supervisés :

- **mon-nginx** : serveur web nginx, exposé sur le port 8081.
- **mon-postgres** : base de données PostgreSQL 18.4, port hôte 5433 mappé sur 5432 interne. Mot de passe : `DockerPass123!`. Contient deux bases : `postgres` (données jeux vidéo) et `assistant_ia` (documentation et historique du chat IA).
- **mon-json-api** : API REST clue/json-server sur le port 8083, sert des données de jeux vidéo (games, studios, personnages, plateformes).

Politique conteneurs : `--restart no`. Les pannes sont gérées par Zabbix (auto-remédiation), pas par Docker. Toujours utiliser `docker start`/`docker stop`, jamais `docker rm` + `docker run` — le changement d'ID casse la découverte Zabbix et détruirait pgvector installé dans mon-postgres.

## Environnement WSL

Machine : DESKTOP-G5B6R19. OS : Ubuntu 24.04 dans WSL2. Réseau en mode miroir (`networkingMode=mirrored` dans `.wslconfig`). IP locale : 192.168.0.163. RAM WSL : 3,8 Go (contrainte serrée). GPU visible dans WSL via CUDA (utilisé par Ollama pour bge-m3).

## Zabbix et interface web

Zabbix 7.0.28 LTS avec PostgreSQL comme backend, Nginx comme reverse proxy, PHP 8.3. Interface web accessible sur http://localhost:8080 (login Admin). L'agent utilisé est zabbix-agent2.

## Rituel de démarrage

À chaque ouverture de WSL, dans l'ordre :

```bash
sudo service postgresql start
sudo systemctl restart zabbix-server zabbix-agent2 nginx php8.3-fpm
~/start-dockers.sh
```

Le script `~/start-dockers.sh` démarre manuellement les conteneurs (pas d'auto-démarrage systemd — retiré volontairement pour que Zabbix reste responsable).

## Base assistant_ia

Base dédiée pour l'assistant IA, séparée des données jeux vidéo. Créée dans mon-postgres avec l'extension pgvector 0.8.4. Deux tables :

- `documentation(id, contenu, source, embedding vector(1024), cree_le)` — chunks de doc pour le RAG.
- `historique_chat(id, question, reponse, cree_le)` — historique des échanges.

pgvector est installé dans la couche inscriptible du conteneur : survit à `stop`/`start`, mais serait perdu par `docker rm`.
