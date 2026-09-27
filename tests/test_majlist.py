"""Tests de /majlist : inventaire lecture seule des versions des conteneurs.

Contrat : docker_updates_list est une action du canal SSH (comme le scan),
mais en commande slash /majlist elle est DIRECTE (pas de OUI) car lecture
seule — le pull des refs d'images ne modifie aucun conteneur.
"""

from app import actions, main as main_mod


def test_detect_majlist_slash():
    label, action = actions.detect_action("/majlist")
    assert action == "docker_updates_list"


def test_detect_majlist_langage_naturel():
    for phrase in (
        "liste les conteneurs et leurs versions",
        "montre-moi les versions des images",
        "affiche les versions des conteneurs",
    ):
        label, action = actions.detect_action(phrase)
        assert action == "docker_updates_list", phrase


def test_majlist_ne_capte_pas_les_autres_intentions():
    # Les phrases scan/apply existantes ne doivent pas basculer sur list
    assert actions.detect_action("check les mises à jour")[1] == "docker_updates_scan"
    assert actions.detect_action("applique les mises à jour")[1] == "docker_updates_apply"


def test_libelle_majlist_precise_le_pull():
    reply = actions.propose("docker_updates_list", "test:user")
    assert "lecture seule" in reply
    actions._PENDING.pop("test:user", None)


def test_majlist_est_une_commande_directe_web():
    assert "/majlist" in main_mod._WEB_COMMANDS
    assert 'data-cmd="/majlist"' in main_mod._WEB_PAGE


async def test_majlist_execute_le_mode_list(monkeypatch):
    from fastapi.testclient import TestClient

    executed = []

    async def fake_execute(action):
        executed.append(action)
        return "    jellyfin : latest (a jour)"

    monkeypatch.setattr(actions, "_execute", fake_execute)
    client = TestClient(main_mod.app)
    r = client.get("/api/majlist", headers={"X-Vektor-Token": "test-token"})
    assert r.status_code == 200
    assert "a jour" in r.json()["report"]
    assert executed == ["docker_updates_list"]
