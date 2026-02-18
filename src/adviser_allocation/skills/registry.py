"""Test skill registry - centralized discovery and management."""

import threading
from typing import Dict, List, Optional

from adviser_allocation.skills.skill import Skill


class SkillRegistry:
    """Central registry for test skill discovery and management.

    Thread-safe singleton that stores and retrieves test skill definitions.
    Uses registry pattern to enable system-wide test discovery.
    """

    _instance: Optional["SkillRegistry"] = None
    _lock = threading.Lock()
    _skills: Dict[str, Skill] = {}

    def __new__(cls) -> "SkillRegistry":
        """Implement thread-safe singleton pattern."""
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
        return cls._instance

    @classmethod
    def register(cls, skill: Skill) -> None:
        """Register a test skill for system discovery.

        Args:
            skill: TestSkill instance to register

        Raises:
            ValueError: If skill with same full_name already registered
        """
        with cls._lock:
            if skill.full_name in cls._skills:
                raise ValueError(f"Skill '{skill.full_name}' already registered")
            cls._skills[skill.full_name] = skill

    @classmethod
    def get_skill(cls, skill_name: str) -> Optional[Skill]:
        """Retrieve a skill by full name or identifier.

        Args:
            skill_name: Full name (category/name) or identifier (name)

        Returns:
            TestSkill if found, None otherwise
        """
        # Try full name first
        if skill_name in cls._skills:
            return cls._skills[skill_name]

        # Try identifier (just the name part)
        for skill in cls._skills.values():
            if skill.identifier == skill_name:
                return skill

        return None

    @classmethod
    def list_skills(
        cls,
        category: Optional[str] = None,
        tags: Optional[List[str]] = None,
    ) -> List[Skill]:
        """List all registered skills, optionally filtered.

        Args:
            category: Filter by category (e.g., 'unit', 'e2e')
            tags: Filter by tags (returns skills matching ANY tag)

        Returns:
            List of Skill instances matching filters
        """
        skills = list(cls._skills.values())

        if category:
            skills = [s for s in skills if s.category == category]

        if tags:
            skills = [s for s in skills if any(tag in s.tags for tag in tags)]

        return skills

    @classmethod
    def get_required_skills(cls) -> List[Skill]:
        """Get all skills required for deployment.

        Returns:
            List of skills with required_for_deployment=True
        """
        return [s for s in cls._skills.values() if s.required_for_deployment]

    @classmethod
    def get_all_skills(cls) -> List[Skill]:
        """Get all registered skills.

        Returns:
            List of all Skill instances
        """
        return list(cls._skills.values())

    @classmethod
    def clear(cls) -> None:
        """Clear all registered skills (useful for testing).

        Warning: This is unsafe in production. Use only in tests.
        """
        with cls._lock:
            cls._skills.clear()

    @classmethod
    def skill_count(cls) -> int:
        """Get total number of registered skills."""
        return len(cls._skills)
