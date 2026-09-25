FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends openssh-client && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt
COPY app ./app
COPY knowledge ./knowledge
# Fichier `version` écrit par deploy/vektor-deploy.sh (tag déployé) :
# wildcard = n'échoue pas en dev local où il est absent. Lu par /ops.
COPY version* ./

RUN useradd --create-home --uid 10001 vektor && chown -R vektor:vektor /app
USER vektor

EXPOSE 8000
# Bind 0.0.0.0 DANS le conteneur : aucun port n'est publié sur l'hôte,
# seule la réseau Docker interne (Caddy) peut joindre l'API.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
