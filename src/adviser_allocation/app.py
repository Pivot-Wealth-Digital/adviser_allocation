"""Application factory."""

import logging
from pathlib import Path

from flask import Flask

from adviser_allocation.utils.secrets import get_secret
from adviser_allocation.utils.common import get_firestore_client


def create_app(config=None):
    """Create and configure Flask application.

    Args:
        config: Optional dictionary of configuration overrides

    Returns:
        Configured Flask app instance
    """
    # Templates/static at project root (3 levels up: src/adviser_allocation/app.py)
    project_root = Path(__file__).parent.parent.parent

    app = Flask(
        __name__,
        template_folder=str(project_root / "templates"),
        static_folder=str(project_root / "static")
    )

    # Configure
    app.secret_key = get_secret("SESSION_SECRET") or "change-me-please"
    if config:
        app.config.update(config)

    # Initialize Firestore
    db = get_firestore_client()

    # Register blueprints
    from adviser_allocation.main import main_bp
    from adviser_allocation.api.box_routes import box_bp
    from adviser_allocation.api.allocation_routes import init_allocation_routes
    from adviser_allocation.api.skills_routes import skills_bp

    app.register_blueprint(main_bp)
    app.register_blueprint(box_bp)
    app.register_blueprint(init_allocation_routes(db))
    app.register_blueprint(skills_bp)

    logging.info(f"Registered {len(app.blueprints)} blueprints")

    return app
