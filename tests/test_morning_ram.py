"""Tests v1.8.2 : ligne RAM VPS du rapport matinal (node_exporter/Prometheus).

La fonction réelle est testée hors CI (Prometheus du VPS) ; ici on vérifie
la mise en récit : seuils narrés, swap/min24h optionnels, silencieux si KO.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from app import morning


def _fake_query(values: dict[str, float | None]):
    """Patch _prom_query : renvoie la valeur selon la sous-chaîne de la query.

    L'ordre du dict compte : les clés plus spécifiques (min_over_time) doivent
    être testées avant les génériques (SwapTotal, MemAvailable)."""
    async def fake(query: str) -> float | None:
        for key, value in values.items():
            if key in query:
                return value
        return None
    return fake


class TestVpsRamLines:
    @pytest.mark.asyncio
    async def test_sain(self):
        with patch.object(morning, "_prom_query", _fake_query({
            "min_over_time": 7.2 * 2**30,
            "SwapTotal": 1.0 * 2**30,
            "MemAvailable": 8.0 * 2**30,
        })):
            lines = await morning._vps_ram_lines()
        assert len(lines) == 1
        assert "🟢" in lines[0]
        assert "8.0G dispo" in lines[0]
        assert "swap 1.0G" in lines[0]
        assert "min 24h : 7.2G" in lines[0]

    @pytest.mark.asyncio
    async def test_marge_moderee(self):
        with patch.object(morning, "_prom_query", _fake_query({
            "MemAvailable": 6.5 * 2**30,
        })):
            lines = await morning._vps_ram_lines()
        assert "🟡" in lines[0]
        assert "marge modérée" in lines[0]

    @pytest.mark.asyncio
    async def test_sous_le_seuil(self):
        with patch.object(morning, "_prom_query", _fake_query({
            "MemAvailable": 4.2 * 2**30,
        })):
            lines = await morning._vps_ram_lines()
        assert "🟡" in lines[0]
        assert "sous le seuil" in lines[0]

    @pytest.mark.asyncio
    async def test_prom_ko_silencieux(self):
        with patch.object(morning, "_prom_query", _fake_query({
            "MemAvailable": None,
        })):
            lines = await morning._vps_ram_lines()
        assert lines == []

    @pytest.mark.asyncio
    async def test_swap_absent_toleré(self):
        with patch.object(morning, "_prom_query", _fake_query({
            "min_over_time": None,
            "SwapTotal": None,
            "MemAvailable": 9.9 * 2**30,
        })):
            lines = await morning._vps_ram_lines()
        assert len(lines) == 1
        assert "swap" not in lines[0]
        assert "min 24h" not in lines[0]


class TestIntegrationRapport:
    @pytest.mark.asyncio
    async def test_ligne_presente_dans_le_rapport(self):
        with patch.object(morning, "_hc_checks", return_value=None), \
             patch.object(morning, "_lvm_lines", return_value=[]), \
             patch.object(morning, "_cis_line", return_value=(False, [])), \
             patch.object(morning, "_vps_ram_lines",
                          return_value=["🟢 RAM VPS : 8.0G dispo, swap 0.1G utilisés (min 24h : 7.2G)"]):
            report = await morning.morning_report()
        assert "RAM VPS" in report
