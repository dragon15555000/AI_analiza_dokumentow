#!/usr/bin/env python3
"""
Produkcyjny entry point dla waitress.
"""

from app import app

if __name__ == "__main__":
    from waitress import serve
    serve(
        app,
        host="0.0.0.0",
        port=5000,
        threads=8,
        url_scheme="http"
    )
