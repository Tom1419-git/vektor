#!/bin/bash
# Backup chiffré des photos Immich (CT 109) vers gdrive via restic.
# - Le montage photos est vu du host via /var/lib/lxc/109/rootfs/mnt/photos
#   (raw LVM monté par le PVE ? NON : c'est le rootfs disk0 ; le mp0 photos
#   est accessible via le device mapper : montage vérifié à l'exécution).
# - Garde : si la source est vide/absente, exit 0 silencieux (photos pas
#   encore importées = rien à faire, pas une erreur).
# - Mot de passe restic : /root/.restic-photos-pass (chmod 600).
# - En cas d'échec : notification Telegram immédiate (succès = silencieux).
set -u

SRC_HOST=/var/lib/lxc/109/rootfs/mnt/photos/immich
# Fallback : monter le LV photos si le path du rootfs est vide
if [ ! -d "$SRC_HOST" ] || [ -z "$(ls -A "$SRC_HOST" 2>/dev/null)" ]; then
  mkdir -p /mnt/photos-109-ro
  mountpoint -q /mnt/photos-109-ro || mount -o ro /dev/pve/vm-109-disk-0 /mnt/photos-109-ro 2>/dev/null || true
  SRC_HOST=/mnt/photos-109-ro/immich
fi

if [ ! -d "$SRC_HOST" ] || [ -z "$(ls -A "$SRC_HOST" 2>/dev/null)" ]; then
  logger -t immich-photos-backup "source vide ou absente ($SRC_HOST) : rien à sauvegarder"
  exit 0
fi

export RESTIC_REPOSITORY=rclone:gdrive:Immich-Photos
export RESTIC_PASSWORD_FILE=/root/.restic-photos-pass
export GOOGLE_APPLICATION_CREDENTIALS=""
export RCLONE_CONFIG=/root/.config/rclone/rclone.conf

mkdir -p /root/.restic-photos-pass 2>/dev/null || true
[ -s /root/.restic-photos-pass ] || echo "$(openssl rand -base64 32)" > /root/.restic-photos-pass
chmod 600 /root/.restic-photos-pass

LOG=/var/log/immich-photos-backup.log
{
  echo "[$(date '+%F %T')] début backup photos immich"
  restic backup "$SRC_HOST" --tag immich-photos 2>&1
  rc=$?
  restic forget --keep-daily 7 --keep-weekly 4 --keep-monthly 6 --prune 2>&1
} >> "$LOG" 2>&1

rc=$(grep -c "^saved snapshot" "$LOG" 2>/dev/null || true)

if ! tail -20 "$LOG" | grep -qE "snapshot [a-f0-9]+ saved"; then
  MSG="🔴 Backup photos Immich en ÉCHEC (voir /var/log/immich-photos-backup.log)"
  curl -s -X POST "https://api.telegram.org/bot$(grep -oE 'bot[0-9]+:[A-Za-z0-9_-]+' /root/.vektor-tg 2>/dev/null | head -1 || true)/sendMessage" -d "chat_id=$(sed -n 2p /root/.vektor-tg 2>/dev/null)" -d "text=$MSG" >/dev/null 2>&1 || true
  logger -t immich-photos-backup "échec du backup restic"
  exit 1
fi
logger -t immich-photos-backup "backup photos OK"
exit 0
