#!/usr/bin/env python3
"""Mesure la latence de qwen2.5:14b avec keep_alive=-1 (résident permanent)."""
import json
import time
import urllib.request

OLLAMA = "http://127.0.0.1:11434"


def run(prompt: str, keep_alive: str) -> dict:
    payload = json.dumps({
        "model": "qwen2.5:14b",
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
        "keep_alive": keep_alive,
        "options": {"temperature": 0.1, "num_predict": 120},
    }).encode()
    req = urllib.request.Request(
        f"{OLLAMA}/api/chat", data=payload,
        headers={"Content-Type": "application/json"},
    )
    t0 = time.perf_counter()
    with urllib.request.urlopen(req, timeout=300) as resp:
        data = json.loads(resp.read())
    elapsed = time.perf_counter() - t0
    return elapsed, (data.get("message", {}).get("content", "") or "").strip()


prompts = [
    "Réponds en français, 2 phrases : quel est le rôle du CT 103 qui héberge Sonarr, Radarr et qBittorrent ?",
    "Réponds en français, 1 phrase : le swap à 42% est-il inquiétant ?",
    "Réponds en français, 1 phrase : donne un exemple de commande systemctl inoffensive.",
    "Réponds en français, 1 phrase : résume pourquoi un VPN protège la vie privée.",
]
for i, p in enumerate(prompts, 1):
    t, _ = run(p, "-1")
    print(f"requête {i} (keep_alive=-1) : {t:.2f}s")
