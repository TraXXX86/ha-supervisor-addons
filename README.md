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
