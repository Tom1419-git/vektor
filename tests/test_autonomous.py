"""v1.7.0 : actions autonomes à graduation de risque.

Contrat de sécurité :
- La classe autonome (snapshots réversibles, inventaires, scan Docker, fstrim)
  s'exécute IMMÉDIATEMENT, sans confirmation OUI.
- Toute action critique (reboot, restart, apply, pause qBit) est REFUSÉE par
  execute_autonomous : elle doit passer par propose() -> OUI.
- detect_action reconnaît les demandes de snapshot (/snap, langage naturel) et
  résout le CT par défaut (103) quand aucun CT n'est précisé.
"""

from app import actions
from app.graph import run_agent


def test_is_autonomous_classe_sure():
    assert actions.is_autonomous("lxc_snap_103_pre-update")
    assert actions.is_autonomous("lxc_snapls")
    assert actions.is_autonomous("docker_updates_scan")
    assert actions.is_autonomous("docker_updates_list")
    assert actions.is_autonomous("pve_fstrim")


def test_is_autonomous_refuse_la_classe_critique():
    for action in (
        "lxc_restart_102",
        "docker_restart_102_jellyfin",
        "docker_updates_apply",
        "qb_pause_103",
        "qb_resume_103",
        "sonarr_rescan",
        "radarr_rescan",
    ):
        assert not actions.is_autonomous(action), action


async def test_execute_autonomous_refuse_critique(monkeypatch):
    async def fail_execute(action):
        raise AssertionError(f"{action} ne doit jamais passer par le canal autonome")

    monkeypatch.setattr(actions, "_execute", fail_execute)
    reply = await actions.execute_autonomous("lxc_restart_102")
    assert "REFUS" in reply
    assert "critique" in reply


def test_detect_snap_slash_avec_ct():
    assert actions.detect_action("/snap 104")[1] == "lxc_snap_104_pre-update"
    assert actions.detect_action("/snap 102")[1] == "lxc_snap_102_pre-update"


def test_detect_snap_sans_ct_resout_le_defaut_103():
    assert actions.detect_action("/snap")[1] == "lxc_snap_103_pre-update"
    assert actions.detect_action("fais un snapshot")[1] == "lxc_snap_103_pre-update"


def test_detect_snap_langage_naturel_avec_ct():
    label, action = actions.detect_action("snapshot du CT 105 avant la mise à jour")
    assert action == "lxc_snap_105_pre-update"


def test_detect_snapls():
    assert actions.detect_action("/snapls")[1] == "lxc_snapls"
    assert actions.detect_action("liste les snapshots")[1] == "lxc_snapls"


def test_snap_ne_capte_pas_les_autres_commandes():
    # /snapshot (sans ct) matche le langage naturel mais PAS /snapls ni /snap 999
    assert actions.detect_action("/snapls")[1] == "lxc_snapls"
    assert actions.detect_action("/snap 999") is None  # CT hors whitelist
    assert actions.detect_action("/status") is None


def test_libelle_snapshot_autonome():
    reply = actions.propose("lxc_snap_103_pre-update", "test:user")
    assert "réversible" in reply and "7 jours" in reply
    actions._PENDING.pop("test:user", None)


async def test_run_agent_execute_snap_sans_confirmation(monkeypatch):
    """Chemin complet : /snap s'exécute immédiatement, sans OUI, sans LLM.

    confirm_pending EST consulté au step 1 (contrat : le OUI n'exécute que
    l'action proposée) mais ne consomme rien ici."""
    executed = []

    async def fake_execute(action):
        executed.append(action)
        return "RESULT: snapshot créé (expiration auto 7 jours)"

    monkeypatch.setattr(actions, "execute_autonomous", fake_execute)

    async def fail_propose(*a, **k):
        raise AssertionError("une action autonome ne doit pas générer de proposition OUI")

    monkeypatch.setattr(actions, "propose", fail_propose)
    reply = await run_agent(memory=None, text="/snap 104", history=[], user_key="tg:1")
    assert executed == ["lxc_snap_104_pre-update"]
    assert "autonome" in reply
    assert "snapshot créé" in reply


async def test_run_agent_laisse_le_flux_critique_intact(monkeypatch):
    """Une action critique propose toujours (jamais d'exécution directe)."""
    proposals = []

    def fake_propose(action, user_key):
        proposals.append(action)
        return "⚠️ Confirme avec OUI"

    monkeypatch.setattr(actions, "propose", fake_propose)

    async def fail_autonomous(action):
        raise AssertionError("un reboot n'est pas une action autonome")

    monkeypatch.setattr(actions, "execute_autonomous", fail_autonomous)
    reply = await run_agent(memory=None, text="redémarre le CT 102", history=[], user_key="tg:1")
    assert proposals == ["lxc_restart_102"]
    assert "OUI" in reply
