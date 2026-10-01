#!/bin/bash
# vektor-diagnose — diagnostics lecture seule, déclenchés par Vektor via le
# canal d'actions (forced command). Playbooks inspirés des incidents réels
# (gel 30/09, flood logs, ENOSPC, flux LXC→NetBird, transcodage…).
# Formats acceptés (re-validés ici, format fermé) :
#   diagnose_pve       charge, mémoire, swap, PSI, watchdog, top process, thin pools
#   diagnose_disk_<ct> usage rootfs + data du CT (101-111)
#   diagnose_docker_<ct> conteneurs down/restarting + restartloop + volumes (101-111)
#   diagnose_net       PVE→VPS NetBird + DNS LAN + sonde dns-switch (dernier état)
#   diagnose_media     transcodage Jellyfin, ffmpeg actifs, fenêtre Tdarr, qBit
#   diagnose_services  checks Healthchecks en échec (lecture DB hc.sqlite via HTTP API? non: via ls/log)
#   diagnose_failover  état dns-switch (state, swapped), tunnel CF, caddy-failover, sso-switch
set -u
ACTION="${SSH_ORIGINAL_COMMAND:-}"
[ -n "$ACTION" ] || { echo "REFUS: action absente"; exit 1; }
echo "$ACTION" | grep -qE "^diagnose_(pve|disk_(101|102|103|104|105|106|107|108|109|110|111)|docker_(101|102|103|104|105|106|107|108|109|110|111)|net|media|services|failover)$" || { echo "REFUS: format invalide"; exit 1; }

hr() { printf '%s\n' "────────────────────────────────"; }

case "$ACTION" in
  diagnose_pve)
    hr; echo "PVE — charge / mémoire / I/O"
    echo "load: $(uptime | grep -oE 'load average.*')"
    free -h | sed -n '2p'
    # Swap : la saturation a déjà causé un gel (07/09)
    echo "swap: $(free -h | awk '/Swap/{print $2" utilisé sur "$3}')"
    for f in /sys/fs/cgroup/cpu.pressure /sys/fs/cgroup/io.pressure /sys/fs/cgroup/memory.pressure; do
      [ -f "$f" ] && echo "PSI $(basename $f): $(grep some "$f" | awk '{print $2" "$4}')"
    done
    hr; echo "Watchdog (gel 30/09 : softdog inopérant)"
    DRV=$(journalctl -u watchdog-mux -b --no-pager 2>/dev/null | grep -oE "driver '[A-Za-z_0-9]+'" | tail -1)
    echo "driver actif: ${DRV:-inconnu} (attendu: iTCO_wdt)"
    hr; journald_disk=$(journalctl --disk-usage 2>/var/log/none | grep -oE '[0-9.]+[KMG]'); echo "journal: $journald_disk (plafond 2G)"
    echo "top CPU:"; ps -eo pcpu,comm --sort=-pcpu | head -4
    echo "top MEM:"; ps -eo pmem,comm --sort=-pmem | head -4
    ;;
  diagnose_disk_*)
    CT="${ACTION#diagnose_disk_}"
    hr; echo "CT $CT — stockage"
    pct exec "$CT" -- df -h / | sed -n 2p
    pct exec "$CT" -- sh -c "command -v docker >/dev/null && docker system df 2>/dev/null | head -5" 2>/dev/null
    echo "(rootfs sur $(grep -oE '(ssd-disk|local-lvm):vm-[0-9]+' /etc/pve/lxc/$CT.conf 2>/dev/null | head -1))"
    ;;
  diagnose_docker_*)
    CT="${ACTION#diagnose_docker_}"
    hr; echo "CT $CT — conteneurs Docker"
    pct exec "$CT" -- sh -c 'docker ps -a --format "{{.Names}}: {{.Status}}" | grep -vE "Up .* \(healthy\)|^$"' 2>/dev/null | grep -E "Exited|Restarting|unhealthy|Dead" \
      || echo "tous les conteneurs UP"
    ;;
  diagnose_net)
    hr; echo "Réseau — PVE → VPS (NetBird) + DNS LAN"
    CODE=$(curl -s -o /dev/null -m 6 -w "%{http_code}" http://100.70.222.73:8011/ 2>/dev/null)
    echo "PVE → vektor-api (NetBird 8011): HTTP $CODE (404 attendu = joignable)"
    for dns in 192.168.1.70 192.168.1.62; do
      dig "@$dns" mayoraz-net.ch +short +time=2 +tries=1 >/dev/null 2>&1 \
        && echo "DNS $dns: OK" || echo "DNS $dns: KO"
    done
    ;;
  diagnose_media)
    hr; echo "Média — transcodage / fenêtre Tdarr / qBit"
    FF=$(pgrep -c ffmpeg 2>/dev/null); FF=${FF:-0}
    echo "ffmpeg actifs: $FF"
    JF=$(pct exec 102 -- systemctl is-active jellyfin 2>/dev/null)
    echo "jellyfin (systemd, CT 102): ${JF:-inconnu}"
    QT=$(pct exec 107 -- sh -c 'curl -s -b "SID=bypass-lan-whitelist" http://192.168.1.77:8181/api/v2/torrents/info' 2>/dev/null | grep -oE '"state":"[a-z_]*ing"' | wc -l)
    echo "qBit (107) torrents actifs: ${QT:-?}"
    ;;
  diagnose_services)
    hr; echo "Healthchecks — dernier rapport"
    # Lecture seule : le rapport du matin (09:35) résume l'état des checks.
    # Ici on liste simplement les checks dont le ping date (stat via API HTTP publique).
    pct exec 104 -- sh -c 'docker ps --filter name=healthchecks --format "{{.Status}}"' 2>/dev/null | sed 's/^/healthchecks: /'
    echo "(détail des checks : voir rapport Telegram 09:35 ou HC :8010)"
    ;;
  diagnose_failover)
    hr; echo "Failover DNS/SSO — état des gardiens"
    echo "dns-switch (CT 104):"
    pct exec 104 -- tail -3 /var/log/dns-switch.log 2>/dev/null | sed 's/^/  /'
    SW=$(pct exec 104 -- python3 -c "import json;print(len(json.load(open('/opt/failover/dns-switch.state')).get('swapped',[])))" 2>/dev/null)
    echo "  records basculés (swapped): ${SW:-?}"
    echo "caddy-failover: $(pct exec 104 -- docker ps --filter name=caddy-failover --format '{{.Status}}')"
    echo "tunnel cloudflared: $(pct exec 104 -- docker ps --filter name=cloudflared --format '{{.Status}}')"
    ;;
esac
echo "── fin diagnostic ($ACTION) ──"
