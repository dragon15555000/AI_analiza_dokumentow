#!/usr/bin/env python3
"""
Produkcyjny entry point dla waitress.
"""

import os

from app import app, APP_HOST

if __name__ == "__main__":
    from waitress import serve
    serve(
        app,
        host=APP_HOST,
        port=int(os.environ.get("APP_PORT", "5000")),
        threads=8,
        url_scheme="http"
    )
