"""Rate limiting middleware for Flask app."""

import logging
from functools import wraps

from flask import Flask, request
from flask_limiter import Limiter
from flask_limiter.errors import RateLimitExceeded
from flask_limiter.util import get_remote_address

logger = logging.getLogger(__name__)


class AppRateLimiter:
    """Rate limiter wrapper for Flask application."""

    def __init__(self, app: Flask = None):
        """Initialize rate limiter.

        Args:
            app: Flask application instance (optional, can call init_app later)
        """
        self.limiter = Limiter(
            key_func=get_remote_address,
            default_limits=["200 per day", "50 per hour"],
            storage_uri="memory://",
        )
        self.app = app
        if app:
            self.init_app(app)

    def init_app(self, app: Flask) -> None:
        """Initialize rate limiter with Flask app.

        Args:
            app: Flask application instance
        """
        self.app = app
        self.limiter.init_app(app)
        app.errorhandler(RateLimitExceeded)(self._handle_rate_limit_exceeded)

    def _handle_rate_limit_exceeded(self, e: RateLimitExceeded):
        """Handle rate limit exceeded errors.

        Args:
            e: RateLimitExceeded exception

        Returns:
            Error response
        """
        logger.warning("Rate limit exceeded for %s: %s", get_remote_address(), str(e))
        return (
            {
                "error": "Rate limit exceeded",
                "message": "Too many requests. Please try again later.",
            },
            429,
        )

    def limit(self, limit_string: str = "50 per hour"):
        """Decorator to apply rate limit to a route.

        Args:
            limit_string: Rate limit string (e.g., "50 per hour", "1000 per day")

        Returns:
            Decorator function
        """
        return self.limiter.limit(limit_string)

    def exempt(self, func):
        """Decorator to exempt a route from rate limiting.

        Args:
            func: Route function to exempt

        Returns:
            Decorated function
        """
        return self.limiter.exempt(func)


# Global rate limiter instance
rate_limiter = AppRateLimiter()


__all__ = [
    "AppRateLimiter",
    "rate_limiter",
]
