#!/usr/bin/env bash
#
# backup-script.sh — Backup Odoo COMPLET (filestore + dump.sql + manifest),
# via l'endpoint natif /web/database/backup, envoyé vers doc-archiver.
#
# ── CONFIGURATION ────────────────────────────────────────────────────────
# TOUT est lu depuis /config/backup-config.env, relu EN CONTINU (avant
# chaque cycle et toutes les 10s pendant l'attente) : intervalle, rétention,
# format, URL Odoo, nom de base, mot de passe maître, URL/token/site de
# l'archiver. Aucune de ces valeurs ne nécessite un rebuild ni un
# redémarrage du conteneur.
#
# Les variables d'environnement de même nom servent uniquement de REPLI
# (compatibilité avec les anciens déploiements pilotés par docker-compose) :
# si une valeur est présente dans le fichier, c'est elle qui gagne.
#
# Le fichier n'est PAS versionné (il contient deux secrets) — voir
# backup-config.env.example.
#
# ── POURQUOI CE SERVICE NE LIT PAS SA CONFIG DEPUIS ODOO NI DEPUIS L'API ──
# Un système de sauvegarde ne doit pas dépendre de ce qu'il sauvegarde :
# si la configuration vivait dans Odoo, elle serait illisible exactement
# quand Odoo est en panne, c'est-à-dire quand on a besoin du backup. Même
# raisonnement, en plus faible, pour l'API doc-archiver. D'où un fichier
# local, sans dépendance réseau pour démarrer.
#
set -uo pipefail

CONFIG_FILE="/config/backup-config.env"

# ── Valeurs par défaut (utilisées seulement si absentes partout) ─────────
BACKUP_INTERVAL_SECONDS=1800
BACKUP_RETENTION_COUNT=12
BACKUP_INCLUDE_FILESTORE=true
ODOO_URL="${ODOO_URL:-}"
ODOO_DB_NAME="${ODOO_DB_NAME:-}"
ODOO_MASTER_PASSWORD="${ODOO_MASTER_PASSWORD:-}"
ARCHIVER_URL="${ARCHIVER_URL:-}"
ARCHIVER_TOKEN="${ARCHIVER_TOKEN:-}"
# ARCHIVER_VILLE : ancien nom, encore accepté en repli pour ne pas casser
# un déploiement existant dont le .env n'a pas encore été renommé.
ARCHIVER_SITE="${ARCHIVER_SITE:-${ARCHIVER_VILLE:-casa}}"

REQUIRED_VARS=(ODOO_URL ODOO_DB_NAME ODOO_MASTER_PASSWORD ARCHIVER_URL ARCHIVER_TOKEN ARCHIVER_SITE)

log() { echo "[BACKUP] $*"; }

# ── Chargement de la configuration ──────────────────────────────────────
# Charge dans un sous-shell d'abord, valide, et ne remplace les valeurs
# courantes que si tout est cohérent. Une config cassée en cours de route
# (faute de frappe, valeur effacée) ne doit JAMAIS interrompre les backups :
# on garde la dernière configuration valide connue et on le signale.
load_config() {
    [ -f "$CONFIG_FILE" ] || return 0

    local tmp_env
    tmp_env=$(mktemp)
    # Le fichier peut être édité sous Windows et arriver en CRLF. Sourcé
    # tel quel, un \r invisible se glisse dans les valeurs numériques et
    # casse silencieusement les comparaisons (-lt) plus bas -> la boucle
    # d'attente s'interrompt après un seul palier de 10s. On nettoie donc
    # systématiquement avant de sourcer.
    tr -d '\r' < "$CONFIG_FILE" > "$tmp_env"

    # shellcheck disable=SC1090
    if ! source "$tmp_env" 2>/dev/null; then
        log "ATTENTION : $CONFIG_FILE illisible — conservation de la configuration précédente"
        rm -f "$tmp_env"
        return 0
    fi
    rm -f "$tmp_env"

    # Un intervalle non numérique ou nul ferait boucler le service à vide.
    if ! [[ "$BACKUP_INTERVAL_SECONDS" =~ ^[0-9]+$ ]] || [ "$BACKUP_INTERVAL_SECONDS" -lt 60 ]; then
        log "ATTENTION : BACKUP_INTERVAL_SECONDS invalide ('${BACKUP_INTERVAL_SECONDS}') — repli sur 1800s"
        BACKUP_INTERVAL_SECONDS=1800
    fi
    if ! [[ "$BACKUP_RETENTION_COUNT" =~ ^[0-9]+$ ]] || [ "$BACKUP_RETENTION_COUNT" -lt 1 ]; then
        log "ATTENTION : BACKUP_RETENTION_COUNT invalide ('${BACKUP_RETENTION_COUNT}') — repli sur 12"
        BACKUP_RETENTION_COUNT=12
    fi
}

check_required() {
    local missing=()
    for var in "${REQUIRED_VARS[@]}"; do
        [ -n "${!var:-}" ] || missing+=("$var")
    done
    if [ ${#missing[@]} -gt 0 ]; then
        log "ERREUR : valeur(s) obligatoire(s) manquante(s) : ${missing[*]}"
        log "         Renseigne-les dans $CONFIG_FILE (cf. backup-config.env.example)"
        return 1
    fi
    return 0
}

# ── Démarrage ───────────────────────────────────────────────────────────
load_config

if [ ! -f "$CONFIG_FILE" ]; then
    log "ATTENTION : $CONFIG_FILE absent — repli sur les variables d'environnement."
    log "            Crée-le depuis backup-config.env.example pour la config à chaud."
fi

if ! check_required; then
    log "Démarrage impossible tant que la configuration est incomplète."
    log "Le service reste actif et réessaiera toutes les 30s (édite le fichier, aucun redémarrage requis)."
    while ! { load_config; check_required; }; do
        sleep 30
    done
    log "Configuration désormais complète — démarrage."
fi

log "Service démarré — backup de la base '${ODOO_DB_NAME}' via ${ODOO_URL}"
log "Destination doc-archiver : ${ARCHIVER_URL} (site=${ARCHIVER_SITE})"
log "Config relue en continu depuis : ${CONFIG_FILE}"
log "Valeurs actuelles : intervalle=${BACKUP_INTERVAL_SECONDS}s, rétention=${BACKUP_RETENTION_COUNT}, filestore=${BACKUP_INCLUDE_FILESTORE}"

CONSECUTIVE_FAILURES=0

while true; do
    load_config
    if ! check_required; then
        log "Configuration devenue incomplète — nouvelle tentative dans 30s"
        sleep 30
        continue
    fi

    # zip  = filestore + dump.sql + manifest.json (restaurable tel quel)
    # dump = SQL seul, sans les fichiers joints
    if [ "${BACKUP_INCLUDE_FILESTORE,,}" = "true" ]; then
        BACKUP_FORMAT="zip"
    else
        BACKUP_FORMAT="dump"
    fi

    TIMESTAMP=$(date +%Y%m%d_%H%M%S)
    # Ce préfixe n'est pas cosmétique : la purge de rétention plus bas
    # s'appuie dessus pour ne toucher QUE les backups produits par ce
    # service (cf. commentaire de la section purge).
    BACKUP_PREFIX="odoo_${ODOO_DB_NAME}_"
    ARCHIVE_FILE="/tmp/${BACKUP_PREFIX}${TIMESTAMP}.${BACKUP_FORMAT}"

    echo ""
    log "$(date '+%Y-%m-%d %H:%M:%S') — appel /web/database/backup (format=${BACKUP_FORMAT})"

    HTTP_CODE=$(curl -s -o "$ARCHIVE_FILE" -w "%{http_code}" \
        -X POST "${ODOO_URL}/web/database/backup" \
        -F "master_pwd=${ODOO_MASTER_PASSWORD}" \
        -F "name=${ODOO_DB_NAME}" \
        -F "backup_format=${BACKUP_FORMAT}")

    # Odoo répond 200 avec une page HTML d'erreur en cas de mauvais mot de
    # passe maître ou de base inexistante — le code HTTP ne suffit donc pas
    # à conclure. Un zip valide commence toujours par les octets "PK" ; un
    # dump SQL Odoo commence par "-- " ou "PGDMP".
    MAGIC=$(head -c 2 "$ARCHIVE_FILE" 2>/dev/null)
    BACKUP_OK=false
    if [ "$HTTP_CODE" = "200" ]; then
        if [ "$BACKUP_FORMAT" = "zip" ] && [ "$MAGIC" = "PK" ]; then
            BACKUP_OK=true
        elif [ "$BACKUP_FORMAT" = "dump" ] && [ -s "$ARCHIVE_FILE" ] && [ "$MAGIC" != "<!" ]; then
            BACKUP_OK=true
        fi
    fi

    if [ "$BACKUP_OK" = true ]; then
        CONSECUTIVE_FAILURES=0
        SIZE=$(du -h "$ARCHIVE_FILE" | cut -f1)
        log "Backup Odoo OK (${SIZE})"

        # NOTE : paramètre 'site' (et non 'ville' — renommé côté API).
        # Pas de is_backup=true ni de folder_prefix=_backups ici, VOLONTAIREMENT :
        # ces deux marqueurs excluent un fichier du mirror du service
        # project-backup de l'API. On veut au contraire que ces zips soient
        # repris par la sauvegarde du projet qui les héberge.
        UPLOAD_CODE=$(curl -s -o /tmp/upload_response.json -w "%{http_code}" \
            -X POST "${ARCHIVER_URL}/documents?site=${ARCHIVER_SITE}" \
            -H "Authorization: Bearer ${ARCHIVER_TOKEN}" \
            -F "files=@${ARCHIVE_FILE}")

        case "$UPLOAD_CODE" in
            200) log "Upload vers doc-archiver : OK" ;;
            401) log "ECHEC upload : token doc-archiver invalide ou expiré (ARCHIVER_TOKEN)" ;;
            403) log "ECHEC upload : ce token n'a pas accès à ce projet" ;;
            507) log "ECHEC upload : quota du projet dépassé côté doc-archiver" ;;
            000) log "ECHEC upload : API doc-archiver injoignable (${ARCHIVER_URL})" ;;
            *)
                log "ECHEC upload (HTTP ${UPLOAD_CODE})"
                head -c 400 /tmp/upload_response.json 2>/dev/null; echo ""
                ;;
        esac
    else
        CONSECUTIVE_FAILURES=$((CONSECUTIVE_FAILURES + 1))
        log "ECHEC backup Odoo (HTTP ${HTTP_CODE}) — échec consécutif n°${CONSECUTIVE_FAILURES}"

        # Diagnostic explicite plutôt qu'un dump HTML brut : le mot de passe
        # maître erroné est de loin la cause la plus fréquente, et c'est une
        # panne silencieuse (le conteneur continue de tourner comme si de
        # rien n'était) — elle doit sauter aux yeux dans les logs.
        RESPONSE_TEXT=$(head -c 2000 "$ARCHIVE_FILE" 2>/dev/null)
        if echo "$RESPONSE_TEXT" | grep -qiE "access denied|master password|mot de passe"; then
            log "  >>> CAUSE PROBABLE : ODOO_MASTER_PASSWORD ne correspond pas à"
            log "      'admin_passwd' de config/odoo.conf."
            log "      Corrige ${CONFIG_FILE} (pris en compte à chaud, sans redémarrage)."
        elif echo "$RESPONSE_TEXT" | grep -qiE "does not exist|database.*not|unknown database"; then
            log "  >>> CAUSE PROBABLE : la base '${ODOO_DB_NAME}' n'existe pas."
            log "      Vérifie ODOO_DB_NAME dans ${CONFIG_FILE}."
        elif [ "$HTTP_CODE" = "000" ]; then
            log "  >>> CAUSE PROBABLE : Odoo injoignable à ${ODOO_URL}."
        else
            log "  Réponse reçue (extrait) :"
            echo "$RESPONSE_TEXT" | head -c 400; echo ""
        fi

        if [ "$CONSECUTIVE_FAILURES" -ge 3 ]; then
            log "  >>> ${CONSECUTIVE_FAILURES} ÉCHECS CONSÉCUTIFS — AUCUN BACKUP N'EST PRODUIT."
        fi
    fi

    rm -f "$ARCHIVE_FILE"

    # ── Purge de rétention ──────────────────────────────────────────────
    # SÉCURITÉ : on ne supprime QUE les fichiers dont le nom porte le
    # préfixe produit par ce service (odoo_<base>_). L'ancienne version
    # listait tous les documents du projet et supprimait tout au-delà du
    # Nième, en hard=true : si ce token servait un jour à autre chose, elle
    # détruisait définitivement de vrais documents. Le filtre par préfixe
    # rend la purge inoffensive pour tout ce qu'elle n'a pas créé.
    if [ "$BACKUP_OK" = true ]; then
        log "Rétention : conserver les ${BACKUP_RETENTION_COUNT} backups les plus récents"
        LIST_JSON=$(curl -s -X GET \
            "${ARCHIVER_URL}/documents?site=${ARCHIVER_SITE}&limit=1000" \
            -H "Authorization: Bearer ${ARCHIVER_TOKEN}")

        OLD_IDS=$(echo "$LIST_JSON" | jq -r \
            --arg prefix "$BACKUP_PREFIX" \
            --argjson keep "$BACKUP_RETENTION_COUNT" '
              [ .[] | select(.filename | startswith($prefix)) ]
              | sort_by(.created_at) | reverse
              | .[$keep:] | .[].id_doc
            ' 2>/dev/null)

        if [ -n "$OLD_IDS" ]; then
            for ID in $OLD_IDS; do
                log "Purge du backup #${ID} (hors rétention)"
                curl -s -o /dev/null -X DELETE \
                    "${ARCHIVER_URL}/documents/${ID}?site=${ARCHIVER_SITE}&hard=true" \
                    -H "Authorization: Bearer ${ARCHIVER_TOKEN}"
            done
        else
            log "Rétention : rien à purger"
        fi
    fi

    # ── Attente, interruptible par un changement de configuration ────────
    TICK=10
    ELAPSED=0
    log "Attente (intervalle courant : ${BACKUP_INTERVAL_SECONDS}s)"
    while [ "$ELAPSED" -lt "$BACKUP_INTERVAL_SECONDS" ]; do
        sleep "$TICK"
        ELAPSED=$((ELAPSED + TICK))
        load_config   # si l'intervalle a été réduit, la boucle en tient compte immédiatement
    done
done
