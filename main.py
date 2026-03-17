"""WSGI entrypoint for Cloud Run.

Gunicorn loads this file as ``main:app``.
The actual application code lives in src/adviser_allocation/.
"""

# Import the app factory from the main module
from adviser_allocation.main import create_app

app = create_app()

__all__ = ["app"]

if __name__ == "__main__":
    import os

    from dotenv import load_dotenv

    load_dotenv()
    app.run(host="0.0.0.0", debug=True, port=int(os.environ.get("PORT", "8080")))
