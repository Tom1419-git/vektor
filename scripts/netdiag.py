#!/usr/bin/env python3
"""Diagnostic réseau : conteneur caddy_default -> IP NetBird du host (Ollama)."""
import json
import socket
import subprocess
import sys

WT0_IP = None
out = subprocess.run(["ip", "-4", "addr", "show", "wt0"], capture_output=True, text=True).stdout
for line in out.splitlines():
    if "inet " in line:
        WT0_IP = line.split()[1].split("/")[0]
print(f"wt0 IP = {WT0_IP}")

CT_GATEWAY = "172.20.0.1"
for target in ["127.0.0.1", "172.17.0.1", "172.20.0.1", WT0_IP]:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(4)
    try:
        s.connect((target, 11434))
        print(f"connect {target}:11434 depuis le host : OK")
    except Exception as exc:
        print(f"connect {target}:11434 depuis le host : {exc.__class__.__name__}")
    finally:
        s.close()

probe = f"""
import socket
for target in ["127.0.0.1", "172.17.0.1", "172.20.0.1", "{WT0_IP}"]:
    s = socket.socket(); s.settimeout(4)
    try:
        s.connect((target, 11434)); print("CT ->", target, "OK")
    except Exception as e:
        print("CT ->", target, e.__class__.__name__)
    finally:
        s.close()
"""
subprocess.run(["docker", "run", "--rm", "--network", "caddy_default", "python:3.12-slim",
                "python", "-c", probe], timeout=60)
