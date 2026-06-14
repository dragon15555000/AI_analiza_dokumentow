#!/usr/bin/env python3
"""
Produkcyjny entry point dla waitress.
Używany przez: python wsgi.py  oraz  python -m waitress ... wsgi:app
"""

import os

from app import APP_HOST, app

if __name__ == "__main__":
    from waitress import serve

    host = os.environ.get("APP_HOST", APP_HOST)
    port = int(os.environ.get("APP_PORT", "5000"))
    threads = int(os.environ.get("WAITRESS_THREADS", "12"))

    serve(
        app,
        host=host,
        port=port,
        threads=threads,
        url_scheme="http",
        ident="ai-analiza",
    )
