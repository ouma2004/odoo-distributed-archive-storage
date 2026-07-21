# -*- coding: utf-8 -*-
from odoo import api, fields, models, _
from odoo.exceptions import ValidationError


class ResUsers(models.Model):
    """
    Extension de res.users : chaque utilisateur choisit où vont SES propres
    nouvelles pièces jointes (Local Storage vs Distributed Archive Storage).

    Contrairement à archive.storage.server (réservé aux administrateurs),
    ces champs sont éditables par l'utilisateur lui-même depuis "Mon Profil"
    — d'où l'extension explicite de SELF_WRITEABLE_FIELDS / SELF_READABLE_FIELDS,
    sans laquelle Odoo rejetterait silencieusement l'écriture non-admin.
    """
    _inherit = "res.users"

    storage_provider = fields.Selection(
        [
            ("local", "Local Storage"),
            ("archive", "Distributed Archive Storage"),
        ],
        string="Storage Provider",
        default="local",
        required=True,
        help="Détermine où seront stockées VOS prochaines pièces jointes. "
             "Les fichiers déjà stockés ne sont pas déplacés rétroactivement.",
    )
    archive_server_id = fields.Many2one(
        "archive.storage.server",
        string="Archive Server",
        domain="[('active', '=', True), "
               "'|', ('allowed_user_ids', '=', False), ('allowed_user_ids', 'in', [uid]), "
               "'|', ('company_id', '=', False), ('company_id', '=', company_id)]",
        help="Serveur d'archivage à utiliser pour vos nouveaux fichiers. "
             "La liste ne montre que les serveurs actifs auxquels vous êtes autorisé.",
    )

    # ── Preuve d'autorisation (cf. archive.storage.server.verification_secret) ──
    archive_access_token = fields.Char(
        string="Archive Verification Secret",
        help="Secret communiqué par l'administrateur, à ressaisir ici pour "
             "prouver que vous êtes autorisé à utiliser le serveur choisi "
             "(uniquement si celui-ci l'exige). Distinct du vrai jeton API "
             "utilisé en interne, que vous n'avez jamais besoin de connaître.",
    )

    @api.onchange("storage_provider")
    def _onchange_storage_provider(self):
        if self.storage_provider != "archive":
            self.archive_server_id = False

    @api.constrains("storage_provider", "archive_server_id", "archive_access_token")
    def _check_archive_server_required_and_authorized(self):
        """
        Triple vérification SERVEUR (jamais seulement côté vue/domaine, qui
        n'est qu'un confort d'affichage et ne protège de rien en RPC direct) :

          1. Un serveur est bien sélectionné si le provider est 'archive'
          2. L'utilisateur fait partie des utilisateurs autorisés (allowed_user_ids)
          3. Si le serveur l'exige (require_user_credentials), les identifiants
             saisis par l'utilisateur correspondent EXACTEMENT à ceux que l'admin
             a configurés sur le serveur — l'utilisateur doit prouver qu'il a bien
             reçu le bon secret par un canal sécurisé, l'admin ne le lui montre
             jamais directement dans l'interface (champs group-restricted).
        """
        import secrets

        for user in self:
            if user.storage_provider != "archive":
                continue

            if not user.archive_server_id:
                raise ValidationError(_(
                    "Vous devez sélectionner un serveur d'archivage lorsque "
                    "le fournisseur de stockage est 'Distributed Archive Storage'."
                ))

            # sudo() nécessaire : ces champs sont group-restricted côté serveur,
            # mais on doit pouvoir les LIRE en interne pour les comparer — on ne
            # les expose jamais à l'utilisateur, on retourne juste match/no-match.
            server = user.archive_server_id.sudo()

            allowed = server.allowed_user_ids
            if allowed and user not in allowed:
                raise ValidationError(_(
                    "Vous n'êtes pas autorisé à utiliser le serveur '%s'."
                ) % server.name)

            if not server.require_user_credentials:
                continue

            if not server.verification_secret:
                # Sécurité activée côté serveur mais aucun secret défini par
                # l'admin : on bloque plutôt que de laisser passer par erreur.
                raise ValidationError(_(
                    "Le serveur '%s' exige une vérification, mais aucun "
                    "'Verification Secret' n'est configuré. Contactez votre "
                    "administrateur."
                ) % server.name)

            if not user.archive_access_token or not secrets.compare_digest(
                user.archive_access_token, server.verification_secret
            ):
                raise ValidationError(_(
                    "Le secret de vérification saisi ne correspond pas à celui "
                    "configuré pour le serveur '%s'. Demandez le bon secret à "
                    "votre administrateur."
                ) % server.name)

    @property
    def SELF_WRITEABLE_FIELDS(self):
        return super().SELF_WRITEABLE_FIELDS + [
            "storage_provider", "archive_server_id", "archive_access_token",
        ]

    @property
    def SELF_READABLE_FIELDS(self):
        return super().SELF_READABLE_FIELDS + [
            "storage_provider", "archive_server_id", "archive_access_token",
        ]
