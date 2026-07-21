# -*- coding: utf-8 -*-
from odoo import api, fields, models
from odoo.exceptions import UserError
from odoo.tools.translate import _


class ArchiveStorageServer(models.Model):
    """
    Représente un serveur d'archivage distribué (une API FastAPI donnée,
    ciblant une 'ville' précise parmi celles qu'elle expose).

    Ce modèle est volontairement dépourvu de toute logique métier de
    stockage : il ne fait que STOCKER la configuration de connexion.
    Toute communication HTTP réelle passe par services/archive_client.py
    (règle n°4 du cahier des charges : aucun modèle ne fait de requête
    HTTP directement).
    """
    _name = "archive.storage.server"
    _description = "Serveur d'archivage distribué"
    _order = "priority asc, name"

    # ── Identification ──────────────────────────────────────────────
    name = fields.Char(
        string="Server Name", required=True,
        help="Nom libre affiché dans Odoo, ex: 'Serveur Rabat - Primaire'.",
    )
    active = fields.Boolean(default=True)
    company_id = fields.Many2one(
        "res.company", string="Company",
        default=lambda self: self.env.company,
        help="Si renseigné, ce serveur n'est visible/utilisable que dans "
             "cette société. Laisser vide pour un serveur partagé entre "
             "toutes les sociétés (multi-société).",
    )

    # ── Connexion (activement utilisés par archive_client.py) ───────
    api_url = fields.Char(
        string="API URL", required=True,
        help="URL de base de l'API FastAPI, ex: https://archive-rabat.exemple.ma",
    )
    city = fields.Char(
        string="City", required=True,
        help=(
            "Valeur technique 'ville' envoyée à l'API (paramètre ?ville=...). "
            "Doit correspondre à une entrée VILLES_ACTIVES du déploiement "
            "FastAPI ciblé par API URL. Vérifiable via 'Tester la connexion'."
        ),
    )
    # ── SUPPRIMÉ : backup_city ────────────────────────────────────────
    # La réplication inter-sites est désormais entièrement pilotée côté
    # serveur (variables Helm {VILLE}_BACKUP_OF / REMOTE_API_xxx), pas au
    # niveau d'un enregistrement Odoo individuel. Forcer une backup_city
    # ici échouerait de toute façon côté API pour un site distant (voir
    # config.py: validate_ville, pas validate_ville_or_remote, sur la
    # route d'override explicite).

    timeout = fields.Integer(
        string="Timeout (s)", default=30,
        help="Délai maximum d'attente pour les appels HTTP vers ce serveur.",
    )
    priority = fields.Integer(
        string="Priority", default=10,
        help="Utilisé pour trier les serveurs proposés à l'utilisateur (plus petit = plus prioritaire).",
    )
    allowed_user_ids = fields.Many2many(
        "res.users", string="Authorized Users",
        help="Si vide : tous les utilisateurs internes peuvent choisir ce serveur. "
             "Si renseigné : seuls les utilisateurs listés peuvent le sélectionner "
             "dans leurs préférences personnelles.",
    )
    deletion_policy = fields.Selection(
        [
            ("soft", "Soft Delete (garder une trace)"),
            ("hard", "Suppression physique (irréversible)"),
        ],
        string="Deletion Policy", default="soft", required=True,
        help="Politique appliquée à TOUS les fichiers de ce serveur lors de "
             "leur suppression dans Odoo. "
             "Soft Delete : le fichier reste dans MinIO, marqué DELETED "
             "(traçabilité, récupérable manuellement côté API). "
             "Suppression physique : le fichier et son entrée PostgreSQL "
             "sont supprimés définitivement — AUCUN retour en arrière possible.",
    )
    require_user_credentials = fields.Boolean(
        string="Require User Credentials",
        help=(
            "Si activé, chaque utilisateur souhaitant utiliser ce serveur doit "
            "ressaisir dans ses préférences personnelles le 'Verification Secret' "
            "défini ci-dessus (PAS l'API Token, qui reste privé). Odoo vérifie "
            "la correspondance exacte avant d'autoriser l'utilisateur."
        ),
    )

    # ── Sécurité (activement utilisé : api_token) ────────────────────
    api_token = fields.Char(
        string="API Token",
        groups="distributed_archive_storage.group_archive_storage_manager",
        help="Jeton Bearer attendu par l'API (Authorization: Bearer <token>). "
             "Peut être un token de projet, un token admin, ou l'ancien token "
             "global (mode legacy) — le type est résolu automatiquement via "
             "'Tester la connexion'.",
    )

    # ── NOUVEAU — résolu automatiquement, jamais saisi à la main ──────
    project_name = fields.Char(
        string="Projet",
        readonly=True,
        help="Nom du projet associé à ce token, résolu automatiquement via "
             "GET /whoami. Vide si mode legacy (token global sans isolation).",
    )
    connection_mode = fields.Selection(
        [
            ("project", "Projet (isolé)"),
            ("admin", "Administrateur (tous projets)"),
            ("legacy", "Legacy (token global)"),
        ],
        string="Mode de connexion", readonly=True,
        help="Type de token détecté lors du dernier test de connexion.",
    )

    # ── Champs réservés (non utilisés par l'API actuelle) ────────────
    verification_secret = fields.Char(
        string="Verification Secret",
        groups="distributed_archive_storage.group_archive_storage_manager",
        help="Secret INDÉPENDANT de l'API Token, à communiquer aux utilisateurs "
             "autorisés par un canal sécurisé. L'API Token réel reste privé et "
             "n'est jamais nécessaire côté utilisateur — seul ce champ sert de "
             "preuve d'autorisation (cf. Require User Credentials).",
    )
    ssl_verify = fields.Boolean(string="Verify SSL", default=True)
    email = fields.Char(string="Contact Email", help="Contact technique responsable de ce serveur (informatif).")

    _sql_constraints = [
        (
            "city_api_url_uniq",
            "unique(city, api_url)",
            "Un serveur avec cette ville et cette API URL existe déjà.",
        ),
    ]

    def action_test_connection(self):
        """
        Bouton 'Tester la connexion'. Délègue entièrement au service HTTP
        centralisé — ce modèle ne doit jamais appeler `requests` lui-même.
        """
        self.ensure_one()
        from ..services.archive_client import ArchiveClient
        from ..services.exceptions import ArchiveConnectionError, ArchiveAuthError, ArchiveError

        client = ArchiveClient.from_server(self)
        try:
            health = client.health_check()
        except ArchiveConnectionError as e:
            raise UserError(_("Connexion échouée : %s") % str(e))
        except ArchiveError as e:
            raise UserError(_(
                "Le serveur d'archivage a répondu de façon inattendue : %s"
            ) % str(e))

        available_cities = health.get("villes", [])
        if self.city not in available_cities:
            raise UserError(_(
                "L'API a répondu, mais la ville configurée '%s' n'existe pas "
                "sur ce déploiement. Villes disponibles : %s"
            ) % (self.city, ", ".join(available_cities)))

        # ── NOUVEAU : résolution du projet via /whoami ────────────────
        try:
            identity = client.whoami()
        except ArchiveConnectionError as e:
            raise UserError(_("Connexion échouée : %s") % str(e))
        except ArchiveAuthError:
            raise UserError(_(
                "Le serveur d'archivage a refusé le jeton d'accès configuré. "
                "Vérifiez le champ 'API Token' de ce serveur, ou demandez un "
                "jeton valide à votre administrateur."
            ))
        except ArchiveError as e:
            raise UserError(_(
                "Le serveur d'archivage a répondu de façon inattendue : %s"
            ) % str(e))

        self.connection_mode = identity.get("mode", "legacy")
        self.project_name = identity.get("project_name") or ""

        mode_label = {
            "admin": _("Administrateur (tous projets visibles)"),
            "project": _("Projet « %s »") % self.project_name,
            "legacy": _("Legacy (token global, sans isolation par projet)"),
        }.get(identity.get("mode"), _("Inconnu"))

        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Connexion réussie"),
                "message": _("Pipeline : %s — Villes : %s — Mode : %s") % (
                    health.get("pipeline", health.get("version", "?")),
                    ", ".join(available_cities), mode_label,
                ),
                "type": "success",
                "sticky": False,
            },
        }