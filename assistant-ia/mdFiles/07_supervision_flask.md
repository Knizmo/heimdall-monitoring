# Couche 5 — Auto-supervision de Flask (boucle méta)

Ce fichier documente la Couche 5 du projet Heimdall : le mécanisme par lequel l'assistant IA (Flask) est lui-même supervisé et auto-remédié par Zabbix. Cette couche boucle le projet en rendant l'observateur (Heimdall) lui-même observable.

## Principe général

Flask, qui fait tourner l'assistant Heimdall, est un service natif WSL2 (Python 3.12) et **n'est pas conteneurisé**. Sa supervision ne peut donc pas passer par la découverte LLD Docker de la Couche 1. À la place, la Couche 5 utilise une supervision applicative HTTP : Zabbix interroge périodiquement les endpoints `/health/*` que Flask expose déjà pour son propre diagnostic, et déclenche une action de remédiation si l'un d'eux ne répond plus 200.

Le résultat est une boucle méta : Heimdall, qui répond à des questions sur la supervision de l'infrastructure, est lui-même supervisé par un mécanisme qu'il documente dans son propre RAG et qu'il peut décrire en langage naturel.

## Architecture de la Couche 5

Le flux complet du mécanisme de supervision et remédiation de Flask est le suivant. Un web scenario Zabbix nommé "Flask Heimdall health" interroge toutes les minutes quatre endpoints Flask : /health/db, /health/ollama, /health/groq, /health/zabbix. Si un des quatre endpoints ne répond pas 200 (deux tentatives), un trigger passe en état PROBLEM avec sévérité High. Une action Zabbix se déclenche alors : elle exécute un script global "Restart Flask" sur l'hôte courant, qui appelle /usr/local/bin/zbx-restart-flask.sh. Ce script tue proprement le processus Python existant, relance Flask via le Python du venv avec détachement complet (setsid + nohup), et attend que /health/db réponde à nouveau. Une fois Flask opérationnel, le trigger revient à l'état RECOVERY et deux notifications sont envoyées : Discord (texte brut) et Email (HTML avec cadre vert).

## Endpoints Flask utilisés comme sondes

Les quatre endpoints suivants, définis dans app.py, servent à la fois pour le diagnostic manuel (curl) et pour les sondes de la Couche 5.

L'endpoint /health/db vérifie la connexion à PostgreSQL (base assistant_ia dans mon-postgres), la présence de l'extension pgvector, et compte les chunks de documentation présents. Renvoie 200 si tout est OK, 500 sinon.

L'endpoint /health/ollama vérifie que le service Ollama tourne sur localhost:11434 et que le modèle bge-m3 est bien chargé. Renvoie 200 si le modèle est présent, 500 sinon.

L'endpoint /health/groq fait un ping léger sur l'API Groq avec la clé configurée dans .env pour valider que la clé fonctionne et que le service est joignable. Renvoie 200 en cas de réponse valide, 500 sinon (clé invalide, quota atteint, panne Groq).

L'endpoint /health/zabbix appelle host.get sur l'API Zabbix avec le token configuré pour vérifier que le token est valide et que l'API répond. Renvoie 200 si l'authentification réussit, 500 sinon.

Le double usage de ces endpoints est intentionnel : ils servent à Heimdall pour vérifier ses propres dépendances au démarrage, et à Zabbix pour surveiller Heimdall en continu.

## Configuration du Web scenario Zabbix

Le web scenario est configuré dans Zabbix via Configuration → Hosts → Zabbix server → Web scenarios → Create web scenario.

Les paramètres généraux sont : Name = "Flask Heimdall health", Update interval = 1m, Attempts = 2 (pour éviter les faux positifs sur une latence ponctuelle), Agent = Zabbix.

Le scenario contient quatre steps, un par endpoint de santé, tous avec un timeout de 10 secondes et une exigence de code HTTP 200 :
- Step 1 : DB check, URL http://localhost:5000/health/db
- Step 2 : Ollama check, URL http://localhost:5000/health/ollama
- Step 3 : Groq check, URL http://localhost:5000/health/groq
- Step 4 : Zabbix check, URL http://localhost:5000/health/zabbix

L'intérêt du découpage en quatre steps distincts est le diagnostic : si Flask tourne mais que Groq est down (clé expirée, quota atteint), le web scenario indique précisément quel step échoue via la macro {ITEM.VALUE}, ce qui remonte jusqu'au trigger et jusqu'à la notification.

## Trigger de détection

Le trigger associé au web scenario est configuré ainsi :

Name : "Flask Heimdall : health check en échec ({ITEM.VALUE})"
Severity : High
Expression : last(/Zabbix server/web.test.fail[Flask Heimdall health])>0

L'item web.test.fail[...] vaut 0 quand tous les steps passent, et prend une valeur positive dès qu'un step échoue. Le trigger passe donc en état PROBLEM dès la première détection d'échec, avec dans son nom le libellé du step défaillant grâce à {ITEM.VALUE}.

## Action Zabbix d'auto-remédiation

L'action Zabbix qui déclenche la remédiation de Flask est configurée dans Alerts → Actions → Trigger actions.

Name : "Flask health failure → remédiation + notif"
Condition : Trigger name contains "Flask Heimdall"

Section Operations (déclenchée à l'entrée en PROBLEM) : une seule opération de type Global Script, exécutant le script "Restart Flask" sur Current host. Pas de notification envoyée à ce stade — on tente d'abord la remédiation automatique silencieusement.

Section Recovery operations (déclenchée au retour en OK) : deux opérations séparées de type Send message. La première envoie une notification Discord (texte brut avec ✅, incluant les macros {HOST.NAME}, {TRIGGER.NAME}, {EVENT.RECOVERY.DATE}, {EVENT.RECOVERY.TIME}, {EVENT.AGE}). La seconde envoie un email HTML avec un cadre vert stylé indiquant que la remédiation a réussi.

Ce pattern (silence en PROBLEM, notification en RECOVERY) évite de spammer l'administrateur pour des incidents résolus automatiquement en moins d'une minute, tout en gardant une trace visible du fait que quelque chose s'est passé.

## Global Script "Restart Flask"

Le script global Zabbix est configuré dans Alerts → Scripts → Create script.

Name : Restart Flask
Scope : Action operation (critique — sans ce scope, le script n'apparaît pas dans le sélecteur des actions)
Type : Script
Execute on : Zabbix agent
Commands : /usr/local/bin/zbx-restart-flask.sh

Pas de paramètre à passer — contrairement au script Docker de la Couche 3 qui reçoit {EVENT.NAME} pour extraire le nom du conteneur, ici il n'y a pas d'ambiguïté : c'est toujours Flask qu'on redémarre.

## Script de remédiation détaillé

Le script /usr/local/bin/zbx-restart-flask.sh, créé avec sudo et rendu exécutable via sudo chmod +x, contient la logique de redémarrage complète.

Il définit d'abord les chemins critiques : USER_HOME="/home/itach", PROJECT_DIR="$USER_HOME/assistant-ia", VENV_PYTHON="$PROJECT_DIR/venv/bin/python3", LOG_FILE="/tmp/flask.log", PID_FILE="/tmp/flask.pid".

Le script log toutes ses actions via la commande logger avec le tag zbx-flask-remediation, ce qui permet de suivre le déroulé dans /var/log/syslog avec la commande sudo tail -f /var/log/syslog | grep zbx-flask-remediation.

La logique métier procède en cinq étapes :

Étape 1 — vérifications préalables : le script vérifie que PROJECT_DIR existe et est accessible, et que VENV_PYTHON existe et est exécutable. Si l'une des deux vérifications échoue, il log ECHEC et sort avec code 1.

Étape 2 — arrêt de l'instance existante : le script appelle pkill -f "python3 app.py" pour tuer tout processus Flask éventuellement encore vivant, puis attend 2 secondes.

Étape 3 — vérification du port libre : le script utilise ss -tlnp pour vérifier que le port 5000 n'est plus occupé. Si le port est encore utilisé après le kill, le script log ECHEC et sort avec code 1.

Étape 4 — relance avec détachement complet : le script exécute cd "$PROJECT_DIR" puis setsid nohup "$VENV_PYTHON" app.py </dev/null >"$LOG_FILE" 2>&1 & pour lancer Flask détaché du processus Zabbix agent parent. Le triple mécanisme setsid + nohup + </dev/null est indispensable : setsid crée une nouvelle session pour que le processus survive à la fin du script Zabbix ; nohup ignore SIGHUP ; </dev/null ferme stdin proprement. Le PID est sauvegardé dans PID_FILE.

Étape 5 — attente de disponibilité : le script boucle jusqu'à 30 fois avec sleep 1, testant à chaque itération curl -sf http://localhost:5000/health/db. Dès que l'endpoint répond 200, le script log SUCCES avec le temps mis (i secondes) et sort avec code 0. Cette boucle couvre le préchauffage bge-m3 (~9 secondes) et évite de considérer la remédiation comme réussie avant que Flask soit réellement fonctionnel. Si la boucle expire sans succès, le script log ECHEC et sort avec code 1.

## Utilisation directe du Python du venv sans activate

Le script appelle directement /home/itach/assistant-ia/venv/bin/python3 au lieu de faire source venv/bin/activate suivi de python3 app.py. Cette approche est plus robuste en contexte non-interactif (Zabbix agent, cron, systemd) pour plusieurs raisons.

Premièrement, la commande source activate modifie les variables d'environnement du shell courant (PATH, VIRTUAL_ENV, PS1), et ces modifications peuvent ne pas se propager correctement dans un contexte non-interactif ou dans un sous-shell.

Deuxièmement, le Python d'un venv est lui-même conscient de son statut de Python de venv : quand on l'exécute directement via son chemin absolu, il configure automatiquement sys.path pour charger les paquets installés dans le venv (dossier venv/lib/python3.X/site-packages) au lieu des paquets système. C'est le mécanisme même de pyvenv, indépendant de activate.

Troisièmement, cette approche évite un shell intermédiaire et donc un point de défaillance : moins de commandes = moins de choses qui peuvent échouer silencieusement.

C'est la méthode recommandée pour lancer un service Python depuis n'importe quel automatisme (cron, systemd, agent de monitoring, script de déploiement).

## Permissions accordées à l'utilisateur zabbix

Le script tourne en tant que l'utilisateur système zabbix, celui qui fait tourner l'agent Zabbix. Par défaut, cet utilisateur n'a aucun accès au dossier /home/itach/, ce qui bloquerait cd $PROJECT_DIR et l'appel du Python du venv.

Pour donner à zabbix un accès minimal au projet sans exposer tout le home de itach, quatre commandes ont été appliquées.

D'abord sudo usermod -aG itach zabbix pour ajouter zabbix au groupe primaire de itach, ce qui donne à zabbix les droits du groupe sur les fichiers de itach.

Ensuite sudo chmod g+x /home/itach pour autoriser le groupe à traverser le dossier home (droit x sans droit r ni w — le groupe peut entrer dans le dossier mais pas en lister le contenu).

Puis chmod -R g+rX /home/itach/assistant-ia pour donner au groupe le droit de lire tous les fichiers du projet et de traverser tous les sous-dossiers. Le X majuscule ne donne le bit d'exécution qu'aux dossiers et aux fichiers déjà exécutables, ce qui évite de rendre exécutables des fichiers .py ou .md par erreur.

Enfin sudo systemctl restart zabbix-agent2 pour que l'agent prenne en compte le nouveau groupe de zabbix (les groupes d'un processus sont lus au démarrage).

La validation de ces permissions se fait en tapant sudo -u zabbix ls /home/itach/assistant-ia (doit lister les fichiers sans erreur) et sudo -u zabbix /home/itach/assistant-ia/venv/bin/python3 -c "print('ok')" (doit afficher "ok").

## Prérequis dans la configuration de l'agent

Le fichier /etc/zabbix/zabbix_agent2.conf doit contenir la directive AllowKey=system.run[*] pour autoriser l'agent à exécuter des commandes arbitraires via les Global Scripts. Cette directive est déjà présente depuis la mise en place de la Couche 3 d'auto-remédiation Docker, donc aucune action supplémentaire n'a été nécessaire pour la Couche 5.

Si la directive était absente ou commentée, le test zabbix_get -s 127.0.0.1 -k 'system.run[whoami]' renverrait "not allowed" au lieu de "zabbix". Après ajout ou modification de la directive, il faut relancer l'agent avec sudo systemctl restart zabbix-agent2.

## Choix architectural — supervision applicative vs conteneurisation

La V6 du projet prévoyait initialement de conteneuriser Flask (créer un Dockerfile, un conteneur nommé flask-assistant avec --restart no) pour l'intégrer à la découverte LLD Docker existante et bénéficier automatiquement de la Couche 3 de remédiation. Ce choix a été abandonné en V7 au profit de la supervision applicative HTTP décrite dans cette Couche 5, pour plusieurs raisons.

Raison technique — Ollama : Ollama tourne nativement sur WSL et écoute par défaut sur 127.0.0.1:11434 seulement, ce qui bloque l'accès depuis un conteneur. Conteneuriser Flask aurait nécessité de reconfigurer Ollama avec OLLAMA_HOST=0.0.0.0:11434, élargissant sa surface d'exposition sur le réseau.

Raison technique — dépendances hôte : Flask consomme trois services natifs WSL (PostgreSQL sur mon-postgres exposé en :5433, Ollama en :11434, API Zabbix en :8080). Conteneurisé, il aurait fallu remplacer localhost par host.docker.internal partout dans .env et lancer le conteneur avec --add-host=host.docker.internal:host-gateway.

Raison pratique — développement du frontend : le fichier static/index.html qui contient l'interface web est modifié régulièrement. Flask natif sert le fichier depuis le disque à chaque requête, ce qui rend chaque modification immédiatement visible. Conteneurisé, chaque modification aurait nécessité soit un rebuild d'image, soit un volume monté (-v ~/assistant-ia/static:/app/static:ro).

Raison ressources — RAM : la machine dispose de 3,8 Go de RAM WSL avec ~2,5 Go libres. Un conteneur Python ajoute ~150 Mo d'overhead pour zéro bénéfice fonctionnel.

Raison architecturale — expressivité : la supervision conteneur ne mesure que la vie du processus (docker ps). La supervision applicative HTTP mesure la vie fonctionnelle du service : /health/groq valide que la clé API fonctionne encore, /health/ollama valide que le modèle bge-m3 est chargé, /health/db valide que pgvector est bien accessible. Ce sont des propriétés plus utiles pour un service de type control plane comme Heimdall.

La contrepartie assumée est un peu plus de code (le script zbx-restart-flask.sh dédié) au lieu de bénéficier gratuitement du mécanisme de la Couche 3. Cette contrepartie est jugée acceptable au vu des cinq raisons ci-dessus.

## Effet de bord pour la démo de soutenance

Tuer Flask pour tester la remédiation de la Couche 5 coupe simultanément deux choses : l'interface web du chatbot (parce que mon-nginx fait un proxy_pass vers un Flask down, il renvoie donc une erreur 502), et l'API /debug/metriques consommée par la zone supervision de l'UI (même raison).

Autrement dit, tester la Couche 5 en direct pendant la démo revient à couper le service qu'on utilise pour observer la Couche 5. C'est un point d'ingénierie intéressant à commenter en soutenance : la supervision d'un composant central pose toujours ce problème de circularité, et le mitiger complètement demanderait une supervision externe indépendante (par exemple un second Zabbix, ou un outil comme UptimeRobot).

Pour la démo, la stratégie recommandée est soit de démontrer via des commandes curl en ligne de commande et sudo tail -f /var/log/syslog | grep zbx-flask-remediation qui restent visibles pendant l'indisponibilité, soit d'avertir le jury à l'avance que l'interface sera inaccessible pendant environ 15 secondes le temps du préchauffage bge-m3 lors de la remédiation.

## Test de bout en bout

Pour valider le fonctionnement complet de la Couche 5, la séquence suivante peut être exécutée à tout moment.

Étape 1 : simuler un crash de Flask avec pkill -f "python3 app.py". Vérifier que curl -s http://localhost:5000/health/db échoue immédiatement.

Étape 2 : attendre 1 à 2 minutes (temps d'un cycle web scenario avec ses 2 attempts). Suivre le déroulé dans les logs avec sudo tail -f /var/log/syslog | grep zbx-flask-remediation. On y voit successivement "Tentative de redémarrage", "Flask lancé (PID XXXX), attente...", puis "SUCCES: opérationnel après Ys".

Étape 3 : vérifier que Flask répond à nouveau avec curl -s http://localhost:5000/health/db | python3 -m json.tool.

Étape 4 : vérifier la réception des notifications RECOVERY sur Discord (message avec ✅) et par email (HTML avec cadre vert).

Étape 5 : vérifier l'action log dans Zabbix via Reports → Action log, où l'action doit apparaître avec status "Executed" (ou "Completed" selon la version).

Si toutes ces étapes réussissent, la Couche 5 est opérationnelle. En cas d'échec à une étape, les points de diagnostic sont : permissions de l'utilisateur zabbix sur /home/itach, présence de AllowKey=system.run[*] dans la config agent, exécutabilité du script de remédiation, et lisibilité du fichier .env par le groupe.
