"""WSGI entrypoint wrapper for App Engine.

This file exists at the project root to maintain backward compatibility
with App Engine's default entrypoint expectation (main:app).

The actual application code lives in src/adviser_allocation/.
"""

from adviser_allocation.app import create_app

app = create_app()

__all__ = ['app']

if __name__ == "__main__":
    import os
    from dotenv import load_dotenv

    load_dotenv()
    app.run(
        host="0.0.0.0",
        debug=True,
        port=int(os.environ.get("PORT", "8080"))
    )
