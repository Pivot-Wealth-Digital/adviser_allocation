"""API blueprints."""

from adviser_allocation.api.webhooks import webhooks_bp, init_webhooks

__all__ = ["webhooks_bp", "init_webhooks"]
