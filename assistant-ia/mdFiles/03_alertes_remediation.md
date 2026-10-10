# Alertes et auto-remédiation

## Canaux de notification

Deux canaux de notification sont configurés : Discord (webhook) et Email (Gmail SMTP). Telegram a été abandonné car il demande des frais SMS au Maroc pour la reconnexion.

## Discord (webhook)

Un serveur Discord privé avec un salon dédié reçoit les alertes. Un webhook a été créé (URL commençant par `https://discord.com/api/webhooks/...`). Le media type Zabbix utilisé est le **Discord natif** (déjà présent dans Zabbix 7.0). Les alertes PROBLEM et RECOVERY sont reçues correctement.

## Email Gmail (SMTP)

Prérequis Gmail : validation en 2 étapes activée + mot de passe d'application 16 caractères (généré sur https://myaccount.google.com/apppasswords).

Configuration du media type "Email Gmail" (Type : Email) :

| Champ | Valeur |
|---|---|
| SMTP server | `smtp.gmail.com` |
| SMTP server port | `587` |
| Email | `Heimdall <alertes@exemple.com>` (le "Heimdall" définit le nom d'expéditeur affiché) |
| SMTP helo | `gmail.com` |
| Connection security | `STARTTLS` |
| Authentication | Username and password |
| Message format | HTML |

Les templates HTML sont stylés : cadre rouge pour PROBLEM, cadre vert pour RECOVERY, tableau d'infos structuré.

## Actions et opérations

Les actions sont créées dans **Alerts → Actions → Trigger actions**. Chaque action a des opérations qui envoient vers Discord ET Email. Les templates de message couvrent Problem (déclenchement) ET Problem recovery (résolution) pour recevoir alerte et résolution.

## Auto-remédiation : architecture

L'auto-remédiation redémarre automatiquement un conteneur tombé sans intervention humaine, puis envoie une notification de résolution. Flux complet :

```
Trigger "n'est plus actif" passe en PROBLEM
   ▼
Action Zabbix (Trigger actions)
   ├─► Operation : Global Script "Restart container" (scope: Action operation)
   │      └─► Zabbix agent exécute /usr/local/bin/zbx-restart-container.sh "{EVENT.NAME}"
   │             └─► docker start <conteneur>
   └─► Recovery operation : notif Discord + Email "Redémarré automatiquement"
```

## Configuration agent — AllowKey

Fichier `/etc/zabbix/zabbix_agent2.conf` : ajouter `AllowKey=system.run[*]` puis `sudo systemctl restart zabbix-agent2`.

Sécurité : `system.run[*]` autorise toute commande depuis le serveur. Acceptable ici car serveur et agent sont sur la même machine WSL. En production distribuée, restreindre à `AllowKey=system.run[docker start *]`.

Ne PAS installer `zabbix-get` via `apt install zabbix-server-*` — ça downgrade Zabbix 7.0 en 6.0.

## Le script de remédiation

Fichier `/usr/local/bin/zbx-restart-container.sh` :

```bash
#!/bin/bash
# Usage: zbx-restart-container.sh "nom_du_trigger"
TRIGGER_NAME="$1"
CONTAINER=$(echo "$TRIGGER_NAME" | grep -oP '/[a-zA-Z0-9_-]+' | head -1 | tr -d '/')

if [ -z "$CONTAINER" ]; then
    logger -t zbx-remediation "ERREUR: nom de conteneur introuvable dans: $TRIGGER_NAME"
    exit 1
fi

logger -t zbx-remediation "Tentative de redemarrage: $CONTAINER"
RESULT=$(docker start "$CONTAINER" 2>&1)
EXIT_CODE=$?

if [ $EXIT_CODE -eq 0 ]; then
    logger -t zbx-remediation "SUCCES: $CONTAINER redemarre"
else
    logger -t zbx-remediation "ECHEC: $CONTAINER - $RESULT"
fi
exit $EXIT_CODE
```

Rendu exécutable : `sudo chmod +x /usr/local/bin/zbx-restart-container.sh`.

Aucune config sudoers nécessaire : le user `zabbix` est déjà dans le groupe `docker` (fait pour docker.info), donc `docker start` fonctionne sans sudo.

## Global Script Zabbix — point clé de déblocage

Créé dans **Alerts → Scripts → Create script** :

| Champ | Valeur |
|---|---|
| Name | `Restart container` |
| Scope | **`Action operation`** (PAS "Manual host action") |
| Type | `Script` |
| Execute on | `Zabbix agent` |
| Commands | `/usr/local/bin/zbx-restart-container.sh "{EVENT.NAME}"` |
| Host group | `All` |
| User group | `All` |

Leçon CRITIQUE (Zabbix 7.0) : dans la fenêtre "Operation details" d'une Action, le sélecteur `Operation` reste grisé sur "Send message" tant qu'aucun script global avec **Scope = Action operation** n'existe. Créer d'abord le script débloque automatiquement le sélecteur.

Utiliser `{EVENT.NAME}` (fiable en 7.0 dans le contexte Action) et non `{TRIGGER.NAME}`.

## L'action d'auto-remédiation

Créée dans **Alerts → Actions → Trigger actions → Create action** :

Onglet Action :
- Name : `Auto-remediation : redemarrer conteneur arrete`
- Condition unique : Type `Event name`, Operator `contains`, Value `n'est plus actif`
- Enabled : oui

Onglet Operations → Operations :
- Operation : `Restart container` (le Global Script)
- Target list : `Current host`
- Steps : 1-1, Step duration : 0

Onglet Operations → Recovery operations : DEUX opérations séparées (une Discord texte brut, une Email HTML avec `<br>` explicites). Leçon : un message unique casse le rendu HTML — l'email HTML ignore les `\n`.

## Test grandeur nature

Séquence observée après un `docker stop mon-nginx` :

| Temps | Événement |
|---|---|
| 0s | `docker stop` exécuté |
| ~30-60s | Trigger "n'est plus actif" apparaît dans Problems |
| Quelques secondes après | Script exécuté (log dans /var/log/syslog : "SUCCES") |
| ~60s après | Trigger repasse OK, notifications Discord + Email reçues |

Commandes de debug :

```bash
sudo tail -f /var/log/syslog | grep zbx-remediation
sudo tail -f /var/log/zabbix/zabbix_server.log | grep -iE "action|command|script"
```

L'historique des actions est consultable dans **Reports → Action log**.

## Effet de bord à connaître

Le trigger "n'est plus actif" couvre TOUS les cas d'arrêt (propre ou brutal). Un `docker stop` volontaire pendant une maintenance déclenchera donc la remédiation. Deux façons de le gérer :

- Mettre l'hôte en **maintenance mode** dans Zabbix pendant l'intervention.
- Désactiver l'action `Auto-remediation` le temps de la manipulation.
