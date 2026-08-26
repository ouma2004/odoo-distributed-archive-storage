# -*- coding: utf-8 -*-
{
    "name": "Distributed Archive Storage",
    "version": "19.0.1.1.0",
    "category": "Discuss",
    "summary": "Utilise un système d'archivage distribué (FastAPI/MinIO) comme Storage Provider pour les pièces jointes Odoo",
    "description": """
Distributed Archive Storage
============================
Ce module ajoute un nouveau Storage Provider pour ir.attachment,
permettant de rediriger le stockage des pièces jointes vers un
système d'archivage distribué externe (API REST FastAPI + MinIO),
au lieu du filestore local d'Odoo.

Le module agit uniquement comme adaptateur (bridge) : toute la
logique de stockage, réplication et haute disponibilité reste
gérée par le système d'archivage externe.
    """,
    "author": "Daisy Consulting",
    "license": "LGPL-3",
    "depends": ["base", "mail"],
    "external_dependencies": {
        "python": ["requests"],
    },
    "data": [
        "security/archive_security.xml",
        "security/ir.model.access.csv",
        "security/ir_rule_ir_attachment.xml",
        "security/ir_rule_archive_storage_server.xml",
        "views/archive_storage_server_views.xml",
        "views/res_users_views.xml",
        "views/ir_attachment_views.xml",
        "views/res_config_settings_views.xml",
    ],
    "installable": True,
    "application": False,
    'assets': {
    'web.assets_backend': [
        'distributed_archive_storage/static/src/js/password_toggle_field.js',
        'distributed_archive_storage/static/src/xml/password_toggle_field.xml',
    ],
   },
}
