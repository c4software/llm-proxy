"""
Point d'entrée : `uvicorn main:app`.

Le code vit dans le paquet llm_proxy/ ; ce fichier n'existe que pour
offrir la cible courte attendue par uvicorn, Docker et l'habitude.
"""

from llm_proxy.app import app

__all__ = ["app"]
