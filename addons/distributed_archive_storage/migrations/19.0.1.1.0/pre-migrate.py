# -*- coding: utf-8 -*-
"""
Renommage du champ `city` en `site` sur archive.storage.server.

Motif : l'API doc-archiver parle de "site" partout (paramètre ?site=...,
SITES_ACTIVES) depuis son renommage ville -> site. Le module gardait le nom
historique `city`, ce qui obligeait à traduire mentalement à chaque lecture.

S'exécute en PRE-migrate, donc AVANT qu'Odoo ne charge le nouveau modèle :
sans ça, Odoo verrait un champ `site` sans colonne correspondante, créerait
une colonne vide, et la valeur de chaque serveur déjà configuré serait
perdue (le champ est `required=True` — les enregistrements existants
deviendraient invalides).

Idempotent : ne fait rien si la colonne `city` n'existe plus (migration
déjà appliquée, ou installation neuve).
"""


def migrate(cr, version):
    if not version:
        # Installation neuve : aucune colonne `city` à renommer.
        return

    cr.execute("""
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'archive_storage_server' AND column_name = 'city'
    """)
    if not cr.fetchone():
        return

    # Si les DEUX colonnes coexistent (cas d'un chargement partiel antérieur),
    # on ne renomme pas : on recopie les valeurs manquantes puis on supprime
    # l'ancienne colonne, pour ne perdre aucune donnée.
    cr.execute("""
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'archive_storage_server' AND column_name = 'site'
    """)
    if cr.fetchone():
        cr.execute("""
            UPDATE archive_storage_server SET site = city
            WHERE site IS NULL AND city IS NOT NULL
        """)
        cr.execute("ALTER TABLE archive_storage_server DROP COLUMN city")
    else:
        cr.execute("ALTER TABLE archive_storage_server RENAME COLUMN city TO site")

    # La contrainte d'unicité porte sur la colonne renommée : on supprime
    # l'ancienne, Odoo recrée la nouvelle (site_api_url_uniq) au chargement.
    cr.execute("""
        ALTER TABLE archive_storage_server
        DROP CONSTRAINT IF EXISTS archive_storage_server_city_api_url_uniq
    """)

    # Trace ORM de l'ancien champ : sans ça, Odoo peut conserver une entrée
    # ir.model.fields fantôme pointant vers une colonne qui n'existe plus.
    cr.execute("""
        DELETE FROM ir_model_fields
        WHERE name = 'city'
          AND model = 'archive.storage.server'
    """)
