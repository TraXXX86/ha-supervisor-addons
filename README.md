# HA Supervisor add-ons

Dépôt Home Assistant de l'add-on **HA Supervisor Agent** : connexion d'une instance
Home Assistant à la plateforme de supervision, télémétrie, inventaire et maintenance
à distance avec consentement.

## Installation

1. Dans Home Assistant, ouvrir **Paramètres → Applications / Modules complémentaires
   → Boutique → ⋮ → Dépôts**.
2. Ajouter `https://github.com/TraXXX86/ha-supervisor-addons`.
3. Installer **HA Supervisor Agent**.
4. Renseigner `server_url` (URL du serveur de supervision accessible depuis Home
   Assistant) et `enroll_code` (code d'enrôlement fourni par ce serveur), puis démarrer
   l'add-on.

La configuration et le fonctionnement du consentement sont décrits dans la
[documentation de l'agent](hasup_agent/DOCS.md).

## Mise à jour vers 0.3.1

Pour **0.4.0**, déployer d'abord serveur/interface avec la migration **0007**.
Cette version ajoute l'inventaire enrichi, l'historique observé et les règles de
protection de l'onglet Sauvegardes. Aucune suppression/restauration n'est ajoutée.
L'inventaire Supervisor ne garantit pas la visibilité sur tous les clouds.

Déployer d'abord le serveur et l'interface compatibles avec Stockage, avec les
migrations 0005 et 0006, puis installer **HA Supervisor Agent 0.3.1**.
La nouvelle collecte horaire fournit la répartition du disque, les principaux
sous-dossiers et l'inventaire des sauvegardes. Les anciennes versions du serveur
refusent ces nouveaux champs. Aucun nouvel enrôlement n'est nécessaire.

## Mise à jour vers 0.2.1

Mettre à jour et redémarrer d'abord le backend depuis `main` du projet principal,
avec sa nouvelle version de `shared/hasup_protocol`, puis actualiser la boutique
Home Assistant et installer **HA Supervisor Agent 0.2.1**. Le backend initial 0.2.0
ne reconnaît pas le nouveau champ d'inventaire. Aucune migration supplémentaire
n'est nécessaire si `0004` est déjà appliquée.

Cette version corrige l'affichage des versions HA après une mise à jour : inventaire
immédiat après une commande réussie, puis vérifications toutes les 15 secondes
pendant deux minutes. La fiche de la plateforme se rafraîchit toutes les 30 secondes.

## Mise à jour initiale vers 0.2.0

Mettre à jour le serveur de supervision **avant l'add-on** : installer ses nouvelles
dépendances, exécuter `alembic upgrade head` depuis `server/`, puis redémarrer le
backend et mettre à jour le frontend. L'agent 0.2.0 utilise le protocole 2 et nécessite
la migration serveur `0004`.

Actualiser ensuite la boutique Home Assistant et installer la mise à jour de l'agent.
Les tunnels exigent le consentement local et une origine dédiée à chaque session.
Le [guide de migration](https://github.com/TraXXX86/HomeAssistantSupervisor/blob/main/docs/runbooks/tunnel-monitoring-v2.md)
détaille la configuration DNS/HTTPS et les essais à effectuer.

Architectures prises en charge : **aarch64** et **amd64**. Home Assistant construit
l'image à partir du Dockerfile lors de l'installation. Un serveur de supervision réel
est nécessaire pour l'enrôlement ; le mock Prism du frontend ne permet pas de connecter
une instance Home Assistant.

## Sources et mises à jour

Les sources sont maintenues dans
[HomeAssistantSupervisor](https://github.com/TraXXX86/HomeAssistantSupervisor), sous
`agent/` et `shared/hasup_protocol/`. Le dossier `hasup_agent/` de ce dépôt est généré :
ne pas y modifier directement les sources.

Pour publier une mise à jour depuis les clones locaux :

1. Modifier les sources dans le projet principal, mettre à jour la version de l'agent
   et son changelog.
2. Depuis la racine du projet principal, exécuter
   `./agent/scripts/sync-addon-repository.sh` et les tests de packaging.
3. Synchroniser le contenu de `addon-repository/` vers le clone de ce dépôt, en
   conservant son dossier `.git` et en retirant les anciens fichiers générés qui ont
   disparu des sources.
4. Vérifier les différences, puis committer et pousser les changements sur `main`.

Le fichier `repository.yaml` reste à la racine de ce dépôt, à côté de `hasup_agent/`,
conformément au [format des dépôts Home Assistant](https://developers.home-assistant.io/docs/apps/repository/).
