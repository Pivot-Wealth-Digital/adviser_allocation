"""E2E and system test skill definitions."""

from skills.decorator import register_test_skill


@register_test_skill(
    name="all_e2e_tests",
    category="e2e",
    description="Run all end-to-end and system tests",
    test_file_pattern="tests/test_*.py",
    proficiency_level="beginner",
    timeout_seconds=900,
    required_for_deployment=False,
    tags=["meta", "aggregate", "optional"],
)
def test_all_e2e_tests():
    """Run all E2E tests."""
    pass
