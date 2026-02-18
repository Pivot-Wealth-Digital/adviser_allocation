"""Decorator for registering test skills."""

from typing import Callable, List, Optional

from adviser_allocation.skills.registry import SkillRegistry
from adviser_allocation.skills.skill import Skill


def register_test_skill(
    name: str,
    category: str,
    description: str,
    test_file_pattern: str,
    proficiency_level: str = "intermediate",
    timeout_seconds: int = 300,
    required_for_deployment: bool = False,
    dependencies: Optional[List[str]] = None,
    tags: Optional[List[str]] = None,
) -> Callable:
    """Decorator to register a function as a test skill.

    Registers the skill in the central registry for system-wide discovery.
    The decorated function can be called directly or via SkillExecutor.

    Args:
        name: Skill identifier (e.g., 'unit_tests_allocation')
        category: Skill category ('unit', 'integration', 'e2e', 'security')
        description: Human-readable description
        test_file_pattern: pytest file pattern to execute
        proficiency_level: 'beginner', 'intermediate', 'advanced'
        timeout_seconds: Max execution time
        required_for_deployment: If True, must pass before deployment
        dependencies: List of skill names this depends on
        tags: List of filter tags

    Returns:
        Decorated function

    Example:
        @register_test_skill(
            name='unit_tests_allocation',
            category='unit',
            description='Unit tests for allocation algorithm',
            test_file_pattern='tests/test_allocation_logic.py',
            required_for_deployment=True,
            tags=['core', 'critical']
        )
        def test_allocation_logic():
            pass
    """

    def decorator(func: Callable) -> Callable:
        """Inner decorator that registers the skill and returns original function."""
        skill = Skill(
            name=name,
            category=category,
            description=description,
            test_file_pattern=test_file_pattern,
            proficiency_level=proficiency_level,
            timeout_seconds=timeout_seconds,
            required_for_deployment=required_for_deployment,
            dependencies=dependencies or [],
            tags=tags or [],
        )

        SkillRegistry.register(skill)

        return func

    return decorator
