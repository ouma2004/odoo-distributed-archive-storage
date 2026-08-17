# Image du service de backup périodique — appelle l'endpoint natif Odoo
# /web/database/backup (zip complet : filestore + dump.sql + manifest.json),
# puis envoie ce zip vers doc-archiver. Plus besoin d'outils Postgres ici :
# Odoo fait le pg_dump lui-même en interne.

FROM debian:bookworm-slim

# curl -> appel HTTP à Odoo + envoi vers l'API doc-archiver
# jq   -> lecture du JSON renvoyé par GET /documents pour la purge de rétention
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl jq ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY backup-script.sh /backup-script.sh
RUN chmod +x /backup-script.sh

ENTRYPOINT ["/backup-script.sh"]