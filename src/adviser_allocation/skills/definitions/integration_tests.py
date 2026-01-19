"""Integration test skill definitions."""

from skills.decorator import register_test_skill


@register_test_skill(
    name="app_e2e",
    category="integration",
    description="End-to-end application tests using Playwright",
    test_file_pattern="tests/test_app_e2e.py",
    proficiency_level="advanced",
    timeout_seconds=600,
    required_for_deployment=True,
    tags=["workflows", "critical", "user-facing"],
)
def test_app_e2e():
    """Run end-to-end application tests."""
    pass


@register_test_skill(
    name="all_integration_tests",
    category="integration",
    description="Run all integration tests",
    test_file_pattern="tests/test_app_e2e.py",
    proficiency_level="beginner",
    timeout_seconds=600,
    required_for_deployment=True,
    dependencies=["integration/app_e2e"],
    tags=["meta", "aggregate"],
)
def test_all_integration_tests():
    """Run all integration tests at once."""
    pass
