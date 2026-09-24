"""Tests du tool-calling natif : périmètre lecture seule et boucle outil.

Le LLM de test est un double minimal (pas d'inférence réelle) qui simule
les réponses de ChatOllama, avec et sans tool_calls.
"""

from app import graph
from tests.conftest import FakeLLM


class _FakeResult:
    def __init__(self, content: str = "", tool_calls: list | None = None) -> None:
        self.content = content
        self.tool_calls = tool_calls or []


class ToolCallingLLM(FakeLLM):
    """Premier appel : demande un outil. Deuxième appel (après injection du
    ToolMessage) : réponse finale qui reprend le résultat de l'outil."""

    async def ainvoke(self, messages):
        self.calls.append(messages)
        has_tool_message = any(getattr(m, "type", "") == "tool" for m in messages)
        if has_tool_message:
            tool_output = next(
                getattr(m, "content", "") for m in messages if getattr(m, "type", "") == "tool"
            )
            return _FakeResult(f"Voici l'état : {tool_output}")
        return _FakeResult(tool_calls=[{"name": "etat_proxmox", "args": {}, "id": "call_1"}])

    def bind_tools(self, tools):
        self.bound.append(list(tools))
        return self


async def test_outils_exposes_lisa_lecture_seule():
    names = {tool_item.name for tool_item in graph.READONLY_TOOLS}
    assert {
        "etat_proxmox", "liste_conteneurs", "etat_stockage",
        "inventaire_docker", "statut_service", "rapport_seeds",
    } <= names
    # Aucune action d'écriture dans le périmètre du LLM
    assert all("restart" not in n and "pause" not in n and "resume" not in n for n in names)
    # Chaque outil expose une docstring (contrat lu par le modèle)
    assert all(tool_item.description for tool_item in graph.READONLY_TOOLS)


async def test_boucle_outil_resultat_injecte(monkeypatch):
    async def fake_summary():
        return "Noeud test : CPU 3%"

    monkeypatch.setattr(graph.t, "pve_summary", fake_summary)
    llm = ToolCallingLLM()
    messages = [{"role": "user", "content": "état de proxmox ?"}]
    answer = await graph._invoke_with_tools(llm, messages)
    # L'outil a été exécuté, son résultat injecté puis reformulé
    assert "CPU 3%" in answer
    # Le second appel LLM contient bien le ToolMessage
    second_call = llm.calls[-1]
    assert any(getattr(m, "type", "") == "tool" for m in second_call)


async def test_outil_inconnu_refuse_pas_casse(monkeypatch):
    async def fail_summary():
        raise AssertionError("outil inconnu ne doit rien exécuter")

    monkeypatch.setattr(graph.t, "pve_summary", fail_summary)

    class UnknownToolLLM(FakeLLM):
        async def ainvoke(self, messages):
            self.calls.append(messages)
            if any(getattr(m, "type", "") == "tool" for m in messages):
                return _FakeResult("je réponds quand même")
            return _FakeResult(tool_calls=[{"name": "formater_le_disque", "args": {}, "id": "call_x"}])

        def bind_tools(self, tools):
            self.bound.append(list(tools))
            return self

    llm = UnknownToolLLM()
    answer = await graph._invoke_with_tools(llm, [])
    assert "non autorisé" in answer or "je réponds quand même" in answer


async def test_outil_en_erreur_reponse_degradee(monkeypatch):
    async def broken_summary():
        raise RuntimeError("API down")

    monkeypatch.setattr(graph.t, "pve_summary", broken_summary)

    class BrokenToolLLM(FakeLLM):
        async def ainvoke(self, messages):
            self.calls.append(messages)
            if any(getattr(m, "type", "") == "tool" for m in messages):
                tool_output = next(getattr(m, "content", "") for m in messages if getattr(m, "type", "") == "tool")
                return _FakeResult(f"constat : {tool_output}")
            return _FakeResult(tool_calls=[{"name": "etat_proxmox", "args": {}, "id": "c1"}])

        def bind_tools(self, tools):
            self.bound.append(list(tools))
            return self

    llm = BrokenToolLLM()
    answer = await graph._invoke_with_tools(llm, [])
    assert "Outil indisponible" in answer


async def test_sans_tool_call_reponse_directe():
    llm = FakeLLM(content="réponse simple")
    answer = await graph._invoke_with_tools(llm, [])
    assert answer == "réponse simple"
    # bind_tools a bien été appelé avec les outils lecture seule
    assert llm.bound and llm.bound[0] == graph.READONLY_TOOLS


class MultiRoundsLLM(FakeLLM):
    """Enchaîne 2 passes d'outils (service puis bibliothèque) avant de répondre —
    le cas « vérifie jellyfin et dis-moi si j'ai le film X »."""

    def __init__(self) -> None:
        super().__init__()
        self.rounds = 0

    async def ainvoke(self, messages):
        self.calls.append(messages)
        self.rounds += 1
        if self.rounds == 1:
            return _FakeResult(tool_calls=[{"name": "statut_service", "args": {"service": "jellyfin"}, "id": "c1"}])
        if self.rounds == 2:
            return _FakeResult(tool_calls=[{"name": "recherche_bibliotheque", "args": {"query": "cars"}, "id": "c2"}])
        tool_outputs = [getattr(m, "content", "") for m in messages if getattr(m, "type", "") == "tool"]
        return _FakeResult(f"réponse multi ({len(tool_outputs)} résultats)")

    def bind_tools(self, tools):
        self.bound.append(list(tools))
        return self


async def test_multi_passes_deux_outils_enchaines(monkeypatch):
    async def fake_check(service):
        return f"{service}: HTTP 200"

    async def fake_search(query):
        return f"1 résultat pour « {query} »"

    monkeypatch.setattr(graph.t, "check_service", fake_check)
    monkeypatch.setattr(graph.t, "jellyfin_search", fake_search)
    llm = MultiRoundsLLM()
    answer = await graph._invoke_with_tools(llm, [])
    # Les DEUX résultats d'outils sont disponibles pour la réponse finale
    assert "(2 résultats)" in answer
    assert llm.rounds == 3


async def test_plafond_de_passes_reponse_quand_meme(monkeypatch):
    """Un modèle qui bouclerait indéfiniment ne bloque pas l'agent."""

    class LoopingLLM(MultiRoundsLLM):
        async def ainvoke(self, messages):
            self.calls.append(messages)
            self.rounds += 1
            return _FakeResult(
                content="je dois encore vérifier",
                tool_calls=[{"name": "etat_proxmox", "args": {}, "id": f"c{self.rounds}"}],
            )

    async def fake_summary():
        return "Noeud test"

    monkeypatch.setattr(graph.t, "pve_summary", fake_summary)
    llm = LoopingLLM()
    answer = await graph._invoke_with_tools(llm, [], max_rounds=3)
    # Au plafond : une dernière inférence produit la réponse (le contenu
    # du modèle, pas un énième tool_call)
    assert answer == "je dois encore vérifier"
    assert llm.rounds == 4  # 3 passes d'outils + 1 appel final


async def test_recherche_bibliotheque_exposee_lecture_seule():
    names = {tool_item.name for tool_item in graph.READONLY_TOOLS}
    assert "recherche_bibliotheque" in names
