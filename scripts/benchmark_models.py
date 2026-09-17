#!/usr/bin/env python3
"""Benchmark Vektor : comparaison de modèles Ollama sur les cas d'usage réels.

Usage : python3 benchmark_models.py [modele1 modele2 ...]
Résultats 16/09/2026 (VPS 12 vCPU, régime permanent keep_alive=-1) :
- llama3.1:8b   : 34s total/4 cas, mais réponses faibles en français (refus bizarres, confusion swap/finance chez 7B)
- qwen2.5:7b    : rapide mais hallucine sur les concepts infra (swap <-> finance)
- qwen2.5:14b   : 51s total/4 cas froid, qualité nettement supérieure => CHOISI, résident en RAM
"""
import json
import sys
import time
import urllib.request

OLLAMA = "http://127.0.0.1:11434"

CASES = [
    ("RAG-infra", "Tu es Vektor. Réponds en français, en 2 phrases maximum.\n\nContexte : CT 103 arr-stack héberge Sonarr, Radarr, Prowlarr, Bazarr, qBittorrent et Tdarr.\n\nQuestion : Quel est le rôle du CT 103 et quels services y tournent ?"),
    ("Diagnostic", "Tu es Vektor. Réponds en français, en 2 phrases maximum.\n\nContexte : Jellyfin tourne sur CT 102. Le swap du serveur est à 42%.\n\nQuestion : Jellyfin rame ce soir, quelles sont les 2 causes les plus probables ?"),
    ("General", "Tu es Vektor. Réponds en français, en 2 phrases maximum.\n\nQuestion : Explique simplement ce qu'est un VPN à quelqu'un qui n'y connaît rien."),
    ("Suivi-contexte", "Tu es Vektor. Réponds en français, en 1 phrase.\n\nContexte : Le CT 103 héberge Sonarr et Radarr.\n\nQuestion : Et Sonarr, il répond bien en ce moment ?"),
]


def run(model: str, prompt: str) -> dict:
    payload = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
        "keep_alive": -1,
        "options": {"temperature": 0.1, "num_predict": 220},
    }).encode()
    req = urllib.request.Request(
        f"{OLLAMA}/api/chat", data=payload,
        headers={"Content-Type": "application/json"},
    )
    t0 = time.perf_counter()
    with urllib.request.urlopen(req, timeout=300) as resp:
        data = json.loads(resp.read())
    elapsed = time.perf_counter() - t0
    return {
        "latency_s": round(elapsed, 2),
        "eval_count": data.get("eval_count", 0),
        "answer": (data.get("message", {}).get("content", "") or "").strip(),
    }


models = sys.argv[1:] or ["qwen2.5:14b"]
for model in models:
    print(f"\n{'=' * 70}\nMODELE : {model}\n{'=' * 70}")
    total = 0.0
    for name, prompt in CASES:
        try:
            r = run(model, prompt)
            total += r["latency_s"]
            print(f"\n--- {name} : {r['latency_s']}s | {r['eval_count']} tokens")
            print(f"    {r['answer'][:200]}")
        except Exception as exc:
            print(f"\n--- {name} : ERREUR {exc.__class__.__name__}")
    print(f"\nTOTAL {model} : {total:.1f}s sur 4 cas")
