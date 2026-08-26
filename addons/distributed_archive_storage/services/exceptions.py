# -*- coding: utf-8 -*-
"""
Exceptions typées pour la communication avec l'API d'archivage distribué.

Objectif : permettre à ir_attachment.py de distinguer précisément les cas
d'erreur (réseau vs auth vs document supprimé vs site indisponible) sans
avoir à inspecter des codes HTTP bruts partout dans le code métier.
"""


class ArchiveError(Exception):
    """Classe de base pour toutes les erreurs liées à l'archivage distant."""


class ArchiveConnectionError(ArchiveError):
    """Le serveur est injoignable (timeout, DNS, connexion refusée)."""


class ArchiveAuthError(ArchiveError):
    """Le token/API key fourni est invalide ou manquant (HTTP 401)."""


class ArchiveNotFoundError(ArchiveError):
    """Le document demandé n'existe pas (HTTP 404)."""


class ArchiveGoneError(ArchiveError):
    """Le document a été soft-deleted côté archive (HTTP 410)."""


class ArchiveUnavailableError(ArchiveError):
    """
    Le site principal ET le site de secours sont indisponibles (HTTP 503).
    Distinct de ArchiveConnectionError : ici l'API a bien répondu, mais
    elle indique elle-même que le document est temporairement inaccessible.
    """


class ArchiveServerError(ArchiveError):
    """Erreur serveur inattendue côté API (HTTP 5xx autre que 503 documenté)."""


class ArchiveValidationError(ArchiveError):
    """Requête malformée ou fichier manquant, rejetée par l'API (HTTP 422)."""
