"""API blueprints."""

from adviser_allocation.api.webhooks import init_webhooks, webhooks_bp

__all__ = ["webhooks_bp", "init_webhooks"]
