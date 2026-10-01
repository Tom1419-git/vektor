"""Tests diagnostics v1.8.0 : actions diagnose_* autonomes (lecture seule).

Le canal d'exécution réel (SSH vers vektor-diagnose sur le PVE) est testé
hors CI ; ici on vérifie le routeur : détection, classe autonome, libellés,
et refus de toute cible hors whitelist.
"""
from app import actions


class TestDetection:
    def test_slash_diag_sans_cible(self):
        assert actions.detect_action("/diag")[1] == "diagnose_pve"

    def test_slash_diag_cible(self):
        for cible in ("pve", "net", "media", "services", "failover"):
            assert actions.detect_action(f"/diag {cible}")[1] == f"diagnose_{cible}"

    def test_slash_diag_ct(self):
        assert actions.detect_action("/diag 104")[1] == "diagnose_docker_104"
        assert actions.detect_action("/diag CT 109")[1] == "diagnose_docker_109"
        assert actions.detect_action("/diag docker 103")[1] == "diagnose_docker_103"

    def test_slash_diag_disque(self):
        assert actions.detect_action("/diag disque 109")[1] == "diagnose_disk_109"
        assert actions.detect_action("/diag disques CT 103")[1] == "diagnose_disk_103"

    def test_langage_naturel(self):
        assert actions.detect_action("fais un diagnostic du pve")[1] == "diagnose_pve"
        assert actions.detect_action("état du réseau")[1] == "diagnose_net"
        assert actions.detect_action("diagnostic jellyfin")[1] == "diagnose_media"
        assert actions.detect_action("état du failover dns")[1] == "diagnose_failover"

    def test_pas_de_faux_positif(self):
        assert actions.detect_action("redémarre jellyfin") is None or \
            actions.detect_action("redémarre jellyfin")[1].startswith("docker_restart")


class TestClasseAutonome:
    def test_toutes_les_diagnose_sont_autonomes(self):
        for a in ("diagnose_pve", "diagnose_net", "diagnose_media",
                  "diagnose_services", "diagnose_failover",
                  "diagnose_docker_104", "diagnose_disk_109"):
            assert actions.is_autonomous(a), a

    def test_ct_hors_whitelist_refuse_par_le_script(self):
        # Côté app, on ne peut PAS générer diagnose_docker_<ct> hors 101-111 :
        # la regex ne capture que 101-111. Vérif :
        assert actions.detect_action("/diag 999") is None
        assert actions.detect_action("/diag 120") is None

    def test_critique_ne_passe_pas_par_le_chemin_autonome(self):
        import asyncio
        result = asyncio.run(actions.execute_autonomous("lxc_restart_103"))
        assert "REFUS" in result


class TestLibelles:
    def test_libelle_humain(self):
        msg = actions.propose.__doc__ and None  # propose() pour critiques only
        # Les diagnose sont autonomes : le libellé passe par propose() quand
        # même (fonction partagée) — vérifions qu'il est francophone.
        text = actions._human_label("diagnose_pve") if hasattr(actions, "_human_label") else None
        # fallback : via propose (enregistre une pending, sans conséquence)
        out = actions.propose("diagnose_pve", "test-key")
        assert "diagnostic" in out.lower()
        actions._PENDING.pop("test-key", None)
