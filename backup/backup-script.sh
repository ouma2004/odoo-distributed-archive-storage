#!/usr/bin/env bash
#
# backup-script.sh — Backup Odoo COMPLET (filestore + dump.sql + manifest),
# via l'endpoint natif /web/database/backup, envoyé vers doc-archiver.
#
# Variables d'environnement fixes (voir docker-compose.yml) :
#   ODOO_URL             -> ex: http://web:8069 (nom du service Odoo dans docker-compose)
#   ODOO_DB_NAME          -> ex: test_clean
#   ODOO_MASTER_PASSWORD  -> mot de passe maître du gestionnaire de bases Odoo
#   ARCHIVER_URL, ARCHIVER_TOKEN, ARCHIVER_VILLE -> API doc-archiver
#
# Paramètres DYNAMIQUES (relus en continu depuis /config/backup-config.env,
# modifiable à chaud sans rebuild/restart) :
#   BACKUP_INTERVAL_SECONDS, BACKUP_RETENTION_COUNT
#
set -uo pipefail

: "${ODOO_URL:?ODOO_URL manquant}"
: "${ODOO_DB_NAME:?ODOO_DB_NAME manquant}"
: "${ODOO_MASTER_PASSWORD:?ODOO_MASTER_PASSWORD manquant}"
: "${ARCHIVER_URL:?ARCHIVER_URL manquant}"
: "${ARCHIVER_TOKEN:?ARCHIVER_TOKEN manquant}"
: "${ARCHIVER_VILLE:=casa}"

CONFIG_FILE="/config/backup-config.env"

# Valeurs par défaut si le fichier de config est absent au démarrage
BACKUP_INTERVAL_SECONDS=1800
BACKUP_RETENTION_COUNT=48

load_config() {
    if [ -f "$CONFIG_FILE" ]; then
        # Le fichier peut être édité sous Windows (Notepad, VS Code en mode
        # CRLF...) et se retrouver avec des fins de ligne \r\n. Sourcé tel
        # quel, ça glisse un \r invisible dans les valeurs numériques, qui
        # casse silencieusement les comparaisons (-lt) plus bas -> la boucle
        # d'attente s'interrompt après un seul palier de 10s au lieu du
        # vrai intervalle. On nettoie donc systématiquement avant de sourcer.
        # shellcheck disable=SC1090
        source <(tr -d '\r' < "$CONFIG_FILE")
    fi
}

load_config
echo "[BACKUP] Service démarré — backup Odoo complet de '${ODOO_DB_NAME}' via ${ODOO_URL}"
echo "[BACKUP] Cible doc-archiver : ${ARCHIVER_URL} (ville=${ARCHIVER_VILLE})"
echo "[BACKUP] Config dynamique : ${CONFIG_FILE} (relue en continu)"
echo "[BACKUP] Valeurs actuelles : intervalle=${BACKUP_INTERVAL_SECONDS}s, rétention=${BACKUP_RETENTION_COUNT}"

while true; do
    load_config   # reprend les valeurs les plus récentes avant chaque cycle

    TIMESTAMP=$(date +%Y%m%d_%H%M%S)
    ZIP_FILE="/tmp/odoo_${ODOO_DB_NAME}_${TIMESTAMP}.zip"

    echo ""
    echo "[BACKUP] $(date '+%Y-%m-%d %H:%M:%S') — appel /web/database/backup -> ${ZIP_FILE}"

    HTTP_CODE=$(curl -s -o "$ZIP_FILE" -w "%{http_code}" \
        -X POST "${ODOO_URL}/web/database/backup" \
        -F "master_pwd=${ODOO_MASTER_PASSWORD}" \
        -F "name=${ODOO_DB_NAME}" \
        -F "backup_format=zip")

    # Un zip valide commence toujours par les octets magiques "PK". Odoo
    # répond parfois 200 avec une page HTML d'erreur (mauvais master_pwd,
    # db inexistante...) -> on vérifie le contenu, pas juste le code HTTP.
    MAGIC=$(head -c 2 "$ZIP_FILE" 2>/dev/null)

    if [ "$HTTP_CODE" = "200" ] && [ "$MAGIC" = "PK" ]; then
        SIZE=$(du -h "$ZIP_FILE" | cut -f1)
        echo "[BACKUP] Backup Odoo OK (${SIZE})"

        UPLOAD_HTTP_CODE=$(curl -s -o /tmp/upload_response.json -w "%{http_code}" \
            -X POST "${ARCHIVER_URL}/documents?ville=${ARCHIVER_VILLE}" \
            -H "Authorization: Bearer ${ARCHIVER_TOKEN}" \
            -F "files=@${ZIP_FILE}")

        if [ "$UPLOAD_HTTP_CODE" = "200" ]; then
            echo "[BACKUP] Upload vers doc-archiver : OK"
        else
            echo "[BACKUP] ECHEC upload (HTTP ${UPLOAD_HTTP_CODE})"
            cat /tmp/upload_response.json 2>/dev/null
        fi
    else
        echo "[BACKUP] ECHEC backup Odoo (HTTP ${HTTP_CODE}, contenu non reconnu comme zip)"
        echo "[BACKUP] Réponse reçue (probablement une erreur Odoo — mot de passe maître ? nom de base ?) :"
        head -c 500 "$ZIP_FILE" 2>/dev/null
        echo ""
    fi

    rm -f "$ZIP_FILE"

    # ── Purge : ne garder que les BACKUP_RETENTION_COUNT backups les plus récents ──
    echo "[BACKUP] Vérification rétention (garder les ${BACKUP_RETENTION_COUNT} plus récents)"
    LIST_JSON=$(curl -s -X GET "${ARCHIVER_URL}/documents?ville=${ARCHIVER_VILLE}&limit=1000" \
        -H "Authorization: Bearer ${ARCHIVER_TOKEN}")

    OLD_IDS=$(echo "$LIST_JSON" | jq -r --argjson n "$BACKUP_RETENTION_COUNT" '.[$n:] | .[].id_doc' 2>/dev/null)

    if [ -n "$OLD_IDS" ]; then
        for ID in $OLD_IDS; do
            echo "[BACKUP] Purge du backup #${ID} (hors rétention)"
            curl -s -o /dev/null -X DELETE \
                "${ARCHIVER_URL}/documents/${ID}?ville=${ARCHIVER_VILLE}&hard=true" \
                -H "Authorization: Bearer ${ARCHIVER_TOKEN}"
        done
    else
        echo "[BACKUP] Rien à purger"
    fi

    # ── Attente interruptible par petits blocs de 10s ──
    TICK=10
    ELAPSED=0
    echo "[BACKUP] Attente (intervalle courant : ${BACKUP_INTERVAL_SECONDS}s)"
    while [ "$ELAPSED" -lt "$BACKUP_INTERVAL_SECONDS" ]; do
        sleep "$TICK"
        ELAPSED=$((ELAPSED + TICK))
        load_config   # relecture -> si l'intervalle a changé, la boucle en tient compte tout de suite
    done
done