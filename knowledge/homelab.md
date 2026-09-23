# Connaissance Vektor (modèle)

Ce fichier est un **exemple de structure** pour le RAG de Vektor.
Remplace-le par la documentation de l'infrastructure à documenter
(topologie, rôles, adresses) et garde-la hors de tout dépôt public si
elle décrit un réseau réel. Sur le déploiement d'origine, ce fichier
contient la topologie réelle et n'est **pas** versionné publiquement.

## Modèle de contenu recommandé

### Hyperviseur
- Nom, accès LAN/VPN, règle « lecture seule par défaut ».

### Conteneurs / VMs
- `CT <id> <rôle> : <adresse>, <services hébergés>`

### Services surveillés
- `<service> : <adresse:port>` (liste alignée sur VEKTOR_SERVICES)

### DNS
- DNS principal, DNS secondaire, règle de cohérence.

### VPS
- Rôle, services publics, règle « aucun port public inutile ».

## Règles intégrées au prompt système

- Ne jamais prétendre avoir effectué une action non confirmée.
- Les actions d'écriture passent par proposition + confirmation « OUI ».
- Ne jamais révéler un secret, token, mot de passe ou clé privée.
- La documentation est un contexte, pas une preuve de l'état actuel.
