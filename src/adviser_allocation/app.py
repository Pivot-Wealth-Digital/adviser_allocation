"""Application factory - compatibility shim for future app factory pattern.

Currently, the main Flask app is initialized in adviser_allocation.main
with all blueprints registered at module load time. This factory function
provides a forward-compatible interface for potential future refactoring.
"""

import logging


def create_app(config=None):
    """Create and configure Flask application.

    For now, this returns the module-level app from main.py.
    In the future, this could create a fresh app instance with blueprints.

    Args:
        config: Optional dictionary of configuration overrides

    Returns:
        Configured Flask app instance
    """
    # Import the pre-configured app from the main module
    # This preserves all initialization and blueprint registration
    from adviser_allocation.main import app as configured_app

    # Apply any configuration overrides if needed
    if config:
        configured_app.config.update(config)

    logging.info(f"Flask app ready with {len(configured_app.blueprints)} blueprints")

    return configured_app
