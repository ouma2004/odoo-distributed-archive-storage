# -*- coding: utf-8 -*-
"""
Client HTTP centralisé pour l'API d'archivage distribué.

RÈGLE D'ARCHITECTURE (cahier des charges, point 4) :
Aucun modèle Odoo ne doit effectuer directement des requêtes HTTP.
Cette classe est le SEUL point d'entrée pour parler à l'API FastAPI.
Elle ne connaît rien d'Odoo (pas d'import odoo.* autre que la traduction) :
elle reçoit une configuration simple et retourne des données Python.
"""
import logging

import requests

from .exceptions import (
    ArchiveError,
    ArchiveConnectionError,
    ArchiveAuthError,
    ArchiveNotFoundError,
    ArchiveGoneError,
    ArchiveUnavailableError,
    ArchiveServerError,
)

_logger = logging.getLogger(__name__)


class ArchiveClient:
    """
    Encapsule tous les appels REST vers un serveur d'archivage donné.

    Usage :
        client = ArchiveClient.from_server(server_record)
        result = client.upload(content, "facture.pdf", "application/pdf")
    """

    def __init__(self, api_url, city, api_token=None,
                 timeout=30, ssl_verify=True):
        self.api_url = api_url.rstrip("/")
        self.city = city
        self.api_token = api_token
        self.timeout = timeout or 30
        self.ssl_verify = ssl_verify

    @classmethod
    def from_server(cls, server):
        """
        Construit un client à partir d'un enregistrement archive.storage.server.
        `server` doit être un recordset Odoo à un seul enregistrement, mais
        cette classe ne dépend d'aucune API Odoo au-delà de la lecture de champs.
        """
        return cls(
            api_url=server.api_url,
            city=server.city,
            api_token=server.api_token,
            timeout=server.timeout,
            ssl_verify=server.ssl_verify,
        )

    # ── Interne ───────────────────────────────────────────────────────
    def _headers(self):
        headers = {}
        if self.api_token:
            headers["Authorization"] = f"Bearer {self.api_token}"
        return headers

    def _request(self, method, path, **kwargs):
        url = f"{self.api_url}{path}"
        try:
            response = requests.request(
                method, url,
                timeout=self.timeout,
                verify=self.ssl_verify,
                headers=self._headers(),
                **kwargs,
            )
        except requests.exceptions.RequestException as e:
            # Le détail technique brut (urllib3, adresses mémoire, etc.) va
            # UNIQUEMENT dans les logs — jamais dans le message remonté à
            # l'utilisateur Odoo, qui doit rester lisible par un non-technicien.
            _logger.error("Archive API injoignable (%s %s) : %s", method, url, e)
            raise ArchiveConnectionError(self._readable_connection_error(e)) from e

        if response.status_code == 401:
            raise ArchiveAuthError("Token d'authentification invalide ou manquant")
        if response.status_code == 403:
            raise ArchiveAuthError("Ce document n'appartient pas au projet de ce token")
        if response.status_code == 404:
            raise ArchiveNotFoundError(self._error_detail(response))
        if response.status_code == 410:
            raise ArchiveGoneError(self._error_detail(response))
        if response.status_code == 503:
            raise ArchiveUnavailableError(self._error_detail(response))
        if 500 <= response.status_code < 600:
            raise ArchiveServerError(self._error_detail(response))
        if not response.ok:
            raise ArchiveError(self._error_detail(response))

        return response

    @staticmethod
    def _readable_connection_error(exc: Exception) -> str:
        """
        Traduit une exception réseau technique (requests/urllib3) en une
        phrase compréhensible par un utilisateur final, sans jargon ni
        adresse mémoire. Le détail technique complet reste dans les logs
        serveur (_logger.error ci-dessus), jamais dans ce message.
        """
        if isinstance(exc, requests.exceptions.ConnectTimeout):
            return "Le serveur d'archivage n'a pas répondu à temps (délai de connexion dépassé)."
        if isinstance(exc, requests.exceptions.ReadTimeout):
            return "Le serveur d'archivage a mis trop de temps à répondre."
        if isinstance(exc, requests.exceptions.Timeout):
            return "Le serveur d'archivage n'a pas répondu à temps."
        if isinstance(exc, requests.exceptions.SSLError):
            return "Erreur de certificat SSL/TLS lors de la connexion au serveur d'archivage."
        if isinstance(exc, requests.exceptions.ConnectionError):
            return ("Impossible de se connecter au serveur d'archivage. "
                    "Vérifiez que l'URL est correcte et que le serveur est démarré.")
        return "Erreur de connexion au serveur d'archivage."

    @staticmethod
    def _error_detail(response):
        """
        Extrait le message d'erreur d'une réponse API. Pour les erreurs
        serveur (5xx), se protège contre un éventuel traceback Python qui
        aurait fuité dans la réponse (ne devrait pas arriver en production,
        mais un utilisateur ne doit JAMAIS voir un traceback dans Odoo).
        """
        try:
            detail = response.json().get("detail", response.text)
        except ValueError:
            detail = response.text

        if response.status_code >= 500:
            looks_like_traceback = (
                not detail
                or len(detail) > 300
                or "Traceback" in detail
                or "  File \"" in detail
            )
            if looks_like_traceback:
                return "Le serveur d'archivage a rencontré une erreur interne inattendue."

        return detail

    # ── Endpoints ─────────────────────────────────────────────────────
    def health_check(self) -> dict:
        response = self._request("GET", "/health")
        return response.json()

    def whoami(self) -> dict:
        """Résout le type de token utilisé (projet / admin / legacy)."""
        response = self._request("GET", "/whoami")
        return response.json()

    def upload(self, content: bytes, filename: str, content_type: str = None) -> dict:
        """
        Envoie un fichier vers POST /documents.

        IMPORTANT — nouveau contrat API (multi-fichiers) : l'API attend
        désormais le champ form-data 'files' (liste), pas 'file' (singulier),
        et renvoie {"uploaded": [...], "errors": [...]} plutôt qu'un objet
        document unique. Cette méthode adapte l'appel single-file existant
        à ce nouveau contrat, et déballe la réponse pour garder la même
        interface de retour qu'avant (un seul dict) côté appelant Odoo.
        """
        params = {"ville": self.city}
        files = {"files": (filename, content, content_type or "application/octet-stream")}
        response = self._request("POST", "/documents", params=params, files=files)
        data = response.json()

        if data.get("errors"):
            detail = data["errors"][0].get("detail", "Erreur inconnue")
            raise ArchiveError(detail)
        if not data.get("uploaded"):
            raise ArchiveError("Réponse API inattendue : aucun document uploadé ni erreur")

        return data["uploaded"][0]

    def upload_multiple(self, files_list: list) -> dict:
        """
        Upload plusieurs fichiers en un seul appel API.
        files_list : liste de tuples (content: bytes, filename: str, content_type: str)
        Retourne le JSON brut {"uploaded": [...], "errors": [...]} — l'appelant
        décide comment traiter les échecs partiels.
        """
        params = {"ville": self.city}
        files = [
            ("files", (filename, content, content_type or "application/octet-stream"))
            for content, filename, content_type in files_list
        ]
        response = self._request("POST", "/documents", params=params, files=files)
        return response.json()

    def download(self, document_id) -> tuple:
        """
        Télécharge un document via GET /documents/{id}.
        Retourne (contenu_bytes, content_type).
        """
        params = {"ville": self.city}
        response = self._request(
            "GET", f"/documents/{document_id}", params=params, stream=True,
        )
        content_type = response.headers.get("content-type", "application/octet-stream")
        return response.content, content_type

    def delete(self, document_id, hard: bool = False) -> dict:
        """
        Supprime un document.
        hard=False (défaut) : soft-delete, DELETE /documents/{id}.
        hard=True : suppression physique irréversible (fichier + entrée DB).
        """
        params = {"ville": self.city, "hard": "true" if hard else "false"}
        response = self._request("DELETE", f"/documents/{document_id}", params=params)
        return response.json()

    def rename(self, document_id, new_filename: str) -> dict:
        """Met à jour le nom affiché côté archive (PATCH /documents/{id})."""
        params = {"ville": self.city}
        response = self._request(
            "PATCH", f"/documents/{document_id}", params=params,
            json={"filename": new_filename},
        )
        return response.json()