# Homelab de Thomas

## Proxmox
- Proxmox est l'hyperviseur physique sur le LAN 192.168.1.60.
- Son IP NetBird est documentée dans la mémoire privée (agy.md), pas dans ce dépôt.
- Les actions Proxmox doivent rester en lecture seule par défaut.

## Conteneurs
- CT 101 network-core : 192.168.1.70, Pi-hole failover et DNS.
- CT 102 media-gpu-lxc : 192.168.1.75, Jellyfin et Tdarr node GPU.
- CT 103 arr-stack : 192.168.1.76, Sonarr, Radarr, Prowlarr, Bazarr et qBittorrent.
- CT 104 tools-lxc : 192.168.1.61, Authelia, SFTPGo, Garmin map et Portainer.
- CT 105 pihole-dash : 192.168.1.65, dashboard Pi-hole.
- CT 106 databases : 192.168.1.80, PostgreSQL et Loki.

## Services
- Jellyfin : 192.168.1.75:8096.
- Sonarr : 192.168.1.76:8989.
- Radarr : 192.168.1.76:7878.
- Prowlarr : 192.168.1.76:9696.
- qBittorrent : 192.168.1.76:8181.
- SFTPGo : 192.168.1.61:8090 pour l'interface web.
- Garmin map : 192.168.1.61:8085.

## DNS
- DNS principal LAN : Raspberry Pi 192.168.1.62.
- DNS secondaire : CT 101 192.168.1.70.
- Le DNS local utilise Pi-hole et les deux nœuds doivent rester cohérents.

## VPS
- Le VPS fournit Caddy, les services publics, le monitoring et Ollama.
- Ollama est accessible localement sur le VPS et ne doit pas être exposé publiquement sans authentification.

## Règles opérationnelles
- Ne jamais supprimer ou redémarrer un service sans confirmation explicite.
- Vérifier l'état réel avec un outil live au lieu de déduire l'état depuis ce document.
- Ne jamais stocker de mot de passe, token ou clé privée dans cette base documentaire.
