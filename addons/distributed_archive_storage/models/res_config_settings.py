# -*- coding: utf-8 -*-
from odoo import api, fields, models


class ResConfigSettings(models.TransientModel):
    """
    Bloc d'aperçu dans Réglages Généraux. Ne remplace PAS le menu dédié
    (Réglages > Archivage Distribué > Serveurs), qui reste le seul endroit
    pour gérer réellement la liste des serveurs (un res.config.settings
    n'est pas fait pour éditer une liste de sous-enregistrements). Ce bloc
    sert uniquement de raccourci / indicateur de configuration.
    """
    _inherit = "res.config.settings"

    archive_server_count = fields.Integer(
        string="Serveurs configurés", compute="_compute_archive_server_count",
    )

    @api.depends("company_id")
    def _compute_archive_server_count(self):
        count = self.env["archive.storage.server"].search_count([])
        for record in self:
            record.archive_server_count = count

    def action_open_archive_servers(self):
        return {
            "type": "ir.actions.act_window",
            "name": "Serveurs d'archivage",
            "res_model": "archive.storage.server",
            "view_mode": "list,form",
        }
