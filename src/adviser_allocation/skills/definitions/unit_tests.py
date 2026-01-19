"""Unit test skill definitions."""

from adviser_allocation.skills.decorator import register_test_skill


@register_test_skill(
    name="allocation_logic",
    category="unit",
    description="Unit tests for core allocation algorithm (allocation.py)",
    test_file_pattern="tests/test_allocation_logic.py",
    proficiency_level="advanced",
    required_for_deployment=True,
    tags=["core", "critical", "algorithm"],
)
def test_allocation_logic():
    """Run unit tests for allocation algorithm."""
    pass


@register_test_skill(
    name="cache_utils",
    category="unit",
    description="Unit tests for caching utilities (cache_utils.py)",
    test_file_pattern="tests/test_cache_utils.py",
    proficiency_level="intermediate",
    required_for_deployment=True,
    tags=["utilities", "performance"],
)
def test_cache_utils():
    """Run unit tests for cache utilities."""
    pass


@register_test_skill(
    name="firestore_helpers",
    category="unit",
    description="Unit tests for Firestore helper functions",
    test_file_pattern="tests/test_firestore_helpers.py",
    proficiency_level="intermediate",
    required_for_deployment=True,
    tags=["database", "persistence"],
)
def test_firestore_helpers():
    """Run unit tests for Firestore helpers."""
    pass


@register_test_skill(
    name="http_client",
    category="unit",
    description="Unit tests for HTTP client with retry logic",
    test_file_pattern="tests/test_http_client.py",
    proficiency_level="intermediate",
    required_for_deployment=True,
    tags=["networking", "resilience"],
)
def test_http_client():
    """Run unit tests for HTTP client."""
    pass


@register_test_skill(
    name="oauth_service",
    category="unit",
    description="Unit tests for OAuth service and token management",
    test_file_pattern="tests/test_oauth_service.py",
    proficiency_level="intermediate",
    required_for_deployment=True,
    tags=["authentication", "security"],
)
def test_oauth_service():
    """Run unit tests for OAuth service."""
    pass


@register_test_skill(
    name="all_unit_tests",
    category="unit",
    description="Run all unit tests",
    test_file_pattern="tests/test_*.py",
    proficiency_level="beginner",
    required_for_deployment=True,
    dependencies=[
        "unit/allocation_logic",
        "unit/cache_utils",
        "unit/firestore_helpers",
        "unit/http_client",
        "unit/oauth_service",
    ],
    tags=["meta", "aggregate"],
)
def test_all_unit_tests():
    """Run all unit tests at once."""
    pass
