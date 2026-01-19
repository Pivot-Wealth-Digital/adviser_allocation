"""Test skill metadata and dataclass definition."""

from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class Skill:
    """Represents a test skill with metadata for discovery and execution."""

    name: str
    """Skill identifier (e.g., 'unit_tests_allocation')"""

    category: str
    """Skill category: 'unit', 'integration', 'e2e', 'security'"""

    description: str
    """Human-readable description of what this skill tests"""

    test_file_pattern: str
    """pytest file pattern (e.g., 'tests/test_allocation_logic.py')"""

    proficiency_level: str = "intermediate"
    """Proficiency level: 'beginner', 'intermediate', 'advanced'"""

    timeout_seconds: int = 300
    """Maximum execution time in seconds"""

    required_for_deployment: bool = False
    """If True, this skill must pass before deployment"""

    dependencies: List[str] = field(default_factory=list)
    """Other skills this depends on (skill names)"""

    tags: List[str] = field(default_factory=list)
    """Additional tags for filtering (e.g., ['core', 'critical', 'api'])"""

    @property
    def full_name(self) -> str:
        """Return fully qualified skill name (category/name)."""
        return f"{self.category}/{self.name}"

    @property
    def identifier(self) -> str:
        """Return short identifier for use in APIs and CLIs."""
        return self.name
