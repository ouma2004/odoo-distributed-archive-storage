# -*- coding: utf-8 -*-
import base64
import hashlib
import logging
import mimetypes

from odoo import api, fields, models, _
from odoo.exceptions import UserError

from ..services.archive_client import ArchiveClient
from ..services.exceptions import (
    ArchiveError,
    ArchiveConnectionError,
    ArchiveAuthError,
    ArchiveNotFoundError,
    ArchiveGoneError,
    ArchiveUnavailableError,
    ArchiveServerError,
)

_logger = logging.getLogger(__name__)

ARCHIVE_FNAME_PREFIX = "archive"


class IrAttachment(models.Model):
    """
    Redirige le contenu binaire des pieces jointes vers le systeme
    d'archivage distribue, quand l'utilisateur courant a choisi ce provider.

    REGLE D'ARCHITECTURE : ce modele ne fait AUCUNE requete HTTP directement.
    Tout passe par ArchiveClient (services/archive_client.py).

    HISTORIQUE DU DIAGNOSTIC (important pour la maintenance future) :
    - create()/write() avec 'raw' dans vals EST le bon point d'interception
      (confirme empiriquement par logs) : Odoo utilise 'raw' (bytes), pas
      'datas' (base64), pour le flux d'upload reel du chatter/composer.
    - _file_write() seul NE SUFFIT PAS : il n'est jamais appele pour ce flux
      dans cette version d'Odoo (verifie empiriquement, aucun log).
    - PROBLEME RESIDUEL : apres avoir pose store_fname via les vals de
      create(), Odoo semble le recalculer/l'ecraser (retour a False) apres
      coup, meme apres une correction via write() ORM classique. On corrige
      donc via UPDATE SQL direct + invalidation de cache, qui contourne
      tout mecanisme de recalcul cote ORM.
    """
    _inherit = "ir.attachment"

    storage_provider = fields.Selection(
        [("local", "Local Storage"), ("archive", "Distributed Archive Storage")],
        string="Storage Provider", default="local", readonly=True,
        help="Provider effectivement utilise pour CE fichier au moment de sa "
             "creation. Ne change pas retroactivement si l'utilisateur change "
             "ses preferences ensuite.",
    )
    archive_document_id = fields.Char(
        string="Archive Document ID", readonly=True, copy=False,
        help="Identifiant du document retourne par l'API d'archivage.",
    )
    archive_server_id = fields.Many2one(
        "archive.storage.server", string="Archive Server",
        readonly=True, copy=False, ondelete="restrict",
        help="Serveur d'archivage sur lequel ce fichier est stocke. "
             "ondelete='restrict' : on ne doit jamais pouvoir supprimer un "
             "serveur tant que des fichiers y sont references.",
    )
    archive_path = fields.Char(
        string="Archive Path", readonly=True, copy=False,
        help="Chemin MinIO renvoye par l'API (informatif / debug).",
    )
    archive_checksum = fields.Char(
        string="Archive Checksum", readonly=True, copy=False,
        help="Checksum SHA-256 calcule par l'API d'archivage. "
             "Distinct du champ natif 'checksum' d'Odoo (SHA1 local).",
    )
    archive_state = fields.Selection(
        [
            ("ARCHIVED", "Archived"),
            ("DELETED", "Deleted (soft)"),
            ("HARD_DELETED", "Deleted (physical)"),
        ],
        string="Archive State", readonly=True, copy=False,
        help="Reflete l'etat cote API. 'DELETED' = soft-delete distant "
             "(fichier toujours dans MinIO mais marque supprime). "
             "'HARD_DELETED' = suppression physique irreversible.",
    )

    @api.model_create_multi
    def create(self, vals_list):
        vals_list = [self._process_archive_upload(vals) for vals in vals_list]
        intended = [
            (v.pop("_archive_fname", None), v.pop("_archive_file_size", None),
             v.pop("_archive_checksum_real", None))
            for v in vals_list
        ]
        records = super().create(vals_list)
        for rec, (fname, file_size, checksum) in zip(records, intended):
            if fname:
                self._force_archive_fields_sql(rec.id, fname, file_size, checksum)
        return records

    def write(self, vals):
        if "datas" in vals or "raw" in vals:
            vals = self._process_archive_upload(vals)
            fname = vals.pop("_archive_fname", None)
            file_size = vals.pop("_archive_file_size", None)
            checksum = vals.pop("_archive_checksum_real", None)
            result = super().write(vals)
            if fname:
                for rec in self:
                    self._force_archive_fields_sql(rec.id, fname, file_size, checksum)
            return result
        return super().write(vals)

    def _force_archive_fields_sql(self, record_id, fname, file_size, checksum):
        """
        UPDATE SQL direct pour store_fname/file_size/checksum, contournant le
        mecanisme de recalcul ORM observe empiriquement : ces 3 champs sont
        derives en interne par Odoo a partir de raw/datas, et sont donc
        ecrases (store_fname=False, file_size=0, checksum vide) des qu'on
        retire raw/datas des vals avant l'enregistrement final. On les force
        ensuite directement en base, puis on invalide le cache ORM pour que
        les lectures ulterieures voient bien les bonnes valeurs.
        """
        self.env.cr.execute(
            "UPDATE ir_attachment SET store_fname = %s, file_size = %s, "
            "checksum = %s WHERE id = %s",
            (fname, file_size or 0, checksum, record_id),
        )
        self.invalidate_model(["store_fname", "file_size", "checksum"])
        _logger.debug(
            "Archive storage: champs appliqués pour l'attachment id=%s "
            "(store_fname=%s, file_size=%s)",
            record_id, fname, file_size,
        )

    def _process_archive_upload(self, vals):
        has_datas = bool(vals.get("datas"))
        has_raw = bool(vals.get("raw"))
        if not (has_datas or has_raw) or vals.get("type") == "url":
            return vals

        _logger.info(
            "ARCHIVE_DEBUG upload candidat: name=%s res_model=%s res_id=%s "
            "res_field=%s mimetype=%s taille=%s",
            vals.get("name"), vals.get("res_model"), vals.get("res_id"),
            vals.get("res_field"), vals.get("mimetype"),
            len(vals["raw"]) if has_raw else len(vals.get("datas", "")),
        )

        if vals.get("res_field"):
            _logger.info("ARCHIVE_DEBUG exclu (res_field=%s)", vals.get("res_field"))
            return vals

        if not vals.get("name"):
            # Écriture technique interne sans aucun nom (ex: pré-calcul anonyme
            # d'une miniature, avant qu'Odoo ne lui attribue son res_field).
            # Une vraie pièce jointe utilisateur a TOUJOURS un nom, quel que
            # soit le widget Odoo utilisé pour l'uploader — ce critère est
            # plus fiable que res_field seul, qui n'est pas encore renseigné
            # à ce stade précis pour ce genre d'écriture.
            _logger.info("ARCHIVE_DEBUG exclu (aucun nom - écriture technique interne)")
            return vals

        user = self.env.user
        if user.storage_provider != "archive" or not user.archive_server_id:
            return vals

        server = user.archive_server_id
        bin_data = vals["raw"] if has_raw else base64.b64decode(vals["datas"])
        checksum = hashlib.sha1(bin_data).hexdigest()

        filename = vals.get("name") or "unnamed.bin"
        mimetype = vals.get("mimetype") or mimetypes.guess_type(filename)[0] \
            or "application/octet-stream"

        client = ArchiveClient.from_server(server)
        try:
            result = client.upload(bin_data, filename, mimetype)
        except ArchiveAuthError:
            raise UserError(_(
                "Authentification refusée par le serveur d'archivage '%s'. "
                "Vérifiez le token configuré (demandez-le à votre administrateur)."
            ) % server.name)
        except ArchiveConnectionError as e:
            # str(e) est déjà un message lisible (traduit dans archive_client.py),
            # pas un traceback technique — sûr à afficher directement.
            raise UserError(_(
                "Impossible d'archiver le fichier : %s"
            ) % str(e))
        except ArchiveError as e:
            raise UserError(_(
                "Échec de l'archivage du fichier sur le serveur '%s'. %s"
            ) % (server.name, str(e)))

        document_id = result["document_id"]
        fname = f"{ARCHIVE_FNAME_PREFIX}:{server.id}:{document_id}"

        vals = dict(vals)
        vals.pop("datas", None)
        vals.pop("raw", None)
        vals.update({
            "db_datas"                 : False,
            "store_fname"              : fname,
            "checksum"                 : checksum,
            "file_size"                : len(bin_data),
            "mimetype"                 : mimetype,
            "type"                     : "binary",
            "storage_provider"         : "archive",
            "archive_document_id"      : str(document_id),
            "archive_server_id"        : server.id,
            "archive_path"             : result.get("archive_path"),
            "archive_checksum"         : result.get("checksum"),
            "archive_state"            : result.get("archive_state", "ARCHIVED"),
            "_archive_fname"           : fname,
            "_archive_file_size"       : len(bin_data),
            "_archive_checksum_real"   : checksum,
        })
        return vals

    def _file_write(self, bin_data, checksum):
        if len(self) != 1 or self.res_field:
            return super()._file_write(bin_data, checksum)

        user = self.env.user
        if user.storage_provider != "archive" or not user.archive_server_id:
            return super()._file_write(bin_data, checksum)

        if self.store_fname and self.store_fname.startswith(f"{ARCHIVE_FNAME_PREFIX}:"):
            return self.store_fname

        server = user.archive_server_id
        filename = self.name or "unnamed.bin"
        mimetype = self.mimetype or mimetypes.guess_type(filename)[0] \
            or "application/octet-stream"

        client = ArchiveClient.from_server(server)
        try:
            result = client.upload(bin_data, filename, mimetype)
        except ArchiveAuthError:
            raise UserError(_(
                "Authentification refusée par le serveur d'archivage '%s'. "
                "Vérifiez le token configuré (demandez-le à votre administrateur)."
            ) % server.name)
        except ArchiveConnectionError as e:
            # str(e) est déjà un message lisible (traduit dans archive_client.py),
            # pas un traceback technique — sûr à afficher directement.
            raise UserError(_(
                "Impossible d'archiver le fichier : %s"
            ) % str(e))
        except ArchiveError as e:
            raise UserError(_(
                "Échec de l'archivage du fichier sur le serveur '%s'. %s"
            ) % (server.name, str(e)))

        document_id = result["document_id"]
        fname = f"{ARCHIVE_FNAME_PREFIX}:{server.id}:{document_id}"

        self.sudo().write({
            "storage_provider"    : "archive",
            "archive_document_id" : str(document_id),
            "archive_server_id"   : server.id,
            "archive_path"        : result.get("archive_path"),
            "archive_checksum"    : result.get("checksum"),
            "archive_state"       : result.get("archive_state", "ARCHIVED"),
        })
        return fname

    def _to_http_stream(self):
        """
        Utilisé par le contrôleur /web/content pour le téléchargement HTTP.
        DÉCOUVERT PAR TRACEBACK : cette méthode ne passe PAS par _file_read().
        Elle suppose toujours que store_fname pointe vers un fichier réel sur
        disque (os.stat(stream.path)) — ce qui casse pour nos fnames
        "archive:...". On redirige vers Stream.from_binary_field, qui lui
        passe par le champ 'raw' -> _compute_raw -> _file_read() (que l'on
        a bien implémenté correctement).

        Contrairement à _file_read (silencieux par design, pour ne jamais
        faire planter un envoi d'email en masse ou un rendu PDF groupé), le
        téléchargement interactif DOIT informer clairement l'utilisateur en
        cas d'échec plutôt que de lui donner un fichier vide sans explication.
        """
        self.ensure_one()
        if self.store_fname and self.store_fname.startswith(f"{ARCHIVE_FNAME_PREFIX}:"):
            from odoo.http import Stream
            server, document_id = self._parse_archive_fname(self.store_fname)
            if server is not None:
                client = ArchiveClient.from_server(server)
                try:
                    client.download(document_id)
                except (ArchiveConnectionError, ArchiveUnavailableError, ArchiveServerError) as e:
                    raise UserError(_(
                        "Le serveur d'archivage '%s' est temporairement injoignable. "
                        "%s Réessayez dans quelques instants."
                    ) % (server.name, str(e)))
                except ArchiveAuthError:
                    raise UserError(_(
                        "Accès refusé par le serveur d'archivage '%s' : le jeton "
                        "d'accès configuré n'est plus valide. Contactez votre "
                        "administrateur pour le faire régulariser."
                    ) % server.name)
                except (ArchiveNotFoundError, ArchiveGoneError) as e:
                    raise UserError(_(
                        "Ce document n'est plus disponible dans l'archive. %s"
                    ) % str(e))
            return Stream.from_binary_field(self, "raw")
        return super()._to_http_stream()

    def _file_read(self, fname):
        if not fname or not fname.startswith(f"{ARCHIVE_FNAME_PREFIX}:"):
            return super()._file_read(fname)

        server, document_id = self._parse_archive_fname(fname)
        if server is None:
            _logger.error("Archive fname invalide ou serveur introuvable : %s", fname)
            return b""

        client = ArchiveClient.from_server(server)
        try:
            content, _content_type = client.download(document_id)
            return content
        except (ArchiveNotFoundError, ArchiveGoneError) as e:
            _logger.warning("Document archive %s indisponible (%s) : %s",
                             document_id, server.name, e)
            return b""
        except (ArchiveConnectionError, ArchiveUnavailableError, ArchiveServerError) as e:
            _logger.error("Lecture archive echouee %s (%s) : %s",
                          document_id, server.name, e)
            return b""
        except ArchiveAuthError as e:
            # Contexte non-interactif (email groupé, rendu PDF en masse) :
            # on ne doit jamais planter ici — on logue clairement pour que
            # l'admin détecte le token expiré/invalide, sans bloquer Odoo.
            _logger.error(
                "Jeton invalide pour le serveur '%s' (doc %s) : %s",
                server.name, document_id, e,
            )
            return b""

    def _file_delete(self, fname):
        if not fname or not fname.startswith(f"{ARCHIVE_FNAME_PREFIX}:"):
            return super()._file_delete(fname)

        server, document_id = self._parse_archive_fname(fname)
        if server is None:
            _logger.error("Archive fname invalide a la suppression : %s", fname)
            return

        client = ArchiveClient.from_server(server)
        hard = server.deletion_policy == "hard"
        try:
            client.delete(document_id, hard=hard)
        except ArchiveNotFoundError:
            pass
        except ArchiveError as e:
            _logger.error("Echec suppression (%s) archive doc %s (%s) : %s",
                          server.deletion_policy, document_id, server.name, e)

    def action_delete_and_return_to_list(self):
        """
        Alternative à l'icône poubelle native du formulaire, dont la
        navigation post-suppression automatique se comporte mal dans le
        contexte de l'action "Mes documents archivés" (formulaire vide
        affiché au lieu de revenir à la liste). Ici, on supprime puis on
        renvoie EXPLICITEMENT vers l'action liste, sans dépendre du
        mécanisme de pagination natif.
        """
        self.ensure_one()
        self.unlink()
        return {
            "type": "ir.actions.act_window",
            "res_model": "ir.attachment",
            "name": "Mes documents archivés",
            "view_mode": "list,form",
            "domain": [("storage_provider", "=", "archive")],
            "target": "current",
        }

    def _parse_archive_fname(self, fname):
        try:
            _prefix, server_id, document_id = fname.split(":", 2)
            server = self.env["archive.storage.server"].sudo().browse(int(server_id))
            if not server.exists():
                return None, None
            return server, document_id
        except (ValueError, IndexError):
            return None, None