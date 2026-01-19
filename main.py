"""WSGI entrypoint wrapper for App Engine.

This file exists at the project root to maintain backward compatibility
with App Engine's default entrypoint expectation (main:app).

The actual application code lives in src/adviser_allocation/.
"""

# Import the pre-configured app from the main module
# This preserves all initialization and blueprint registration from src/adviser_allocation/main.py
from adviser_allocation.main import app

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
