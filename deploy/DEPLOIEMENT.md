# Déploiement Vektor (v1.5.3+)

## Modèle : pull-based, CI-gated, zéro secret chez GitHub

```
GitHub (tags v*) ──HTTPS public──> VPS (watcher systemd toutes les 5 min)
                                        │
                          CI verte ? ───┤ (API GitHub publique, sans auth)
                                        ▼
                        /opt/vektor-releases/<tag>/ + docker compose up
                                        │
                          /health OK ? ─┤ non → ROLLBACK auto (release N-1)
                                        ▼
                                 fichier `version` = tag déployé
```

Pourquoi ce modèle :

- **Aucune clé SSH privée dans les secrets GitHub** : le VPS tire en HTTPS
  sortant, GitHub ne peut jamais initier une connexion vers le VPS. Un
  compte GitHub compromis ne donne aucun accès à l'infrastructure.
- **CI-gated** : un tag dont les checks ne sont pas tous `success` n'est
  jamais déployé.
- **Secrets inviolés** : `.env` et `secrets/` vivent uniquement dans
  `/opt/vektor` (chmod 600 / 700), copiés dans le nouveau release au
  déploiement, jamais committés ni téléchargés.

## Installation (une fois, sur le VPS)

```bash
# Depuis le release courant (ou clonage ponctuel) :
install -m 0755 deploy/vektor-deploy.sh /usr/local/bin/vektor-deploy
install -m 0644 deploy/vektor-deploy.timer deploy/vektor-deploy.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now vektor-deploy.timer
```

Vérifier : `systemctl status vektor-deploy.timer` puis
`journalctl -u vektor-deploy -f` au prochain tag.

Note : le health check post-deploy s'exécute **dans le conteneur**
(`docker exec vektor-api python` vers `127.0.0.1:8000/health`) car l'API
ne publie aucun port vers l'hôte — seule Caddy la joignait auparavant via
le réseau Docker.

## Déployer une version

```bash
git tag v1.5.4 && git push origin v1.5.4   # CI verte → déployé en ≤ 5 min
```

Déploiement immédiat sans tag (test, hotfix) :

```bash
vektor-deploy --force      # déploie HEAD du repo, CI non vérifiée
```

## Rollback

Chaque release reste dans `/opt/vektor-releases/<tag>/` (avec son `.env`
et ses secrets copiés). Retour arrière :

```bash
cd /opt/vektor-releases/<tag-précédent>
docker compose -f compose.yml --profile telegram up -d
```

Le rollback automatique (health check KO 90 s après le up) fait exactement
cela vers le tag contenu dans `/opt/vektor/version` avant le déploiement.

## Notes sécurité

- Le script refuse de tourner sans `.env` et se verrouille (`flock`) :
  pas de déploiements concurrents.
- Les logs vont dans `journalctl -u vektor-deploy` : aucune valeur secrète
  n'y est imprimée (seulement des tags, chemins et verdicts).
- `--force` court-circuite la vérification CI : à réserver aux cas où le
  déploiement de HEAD est assumé.
- Les anciens releases s'accumulent dans `/opt/vektor-releases` :
  purger périodiquement (garder les 3-4 derniers suffit au rollback).

## Ce que ce dossier ne contient pas

Aucun secret, aucune adresse privée, aucun identifiant : tout ce qui est
spécifique à l'infrastructure (IP, tokens, hosts) reste dans `.env`,
`secrets/` ou la mémoire locale hors git.
