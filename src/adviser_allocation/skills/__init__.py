"""Test skill framework - centralized test discovery and execution."""

from adviser_allocation.skills.skill import Skill
from adviser_allocation.skills.registry import SkillRegistry
from adviser_allocation.skills.decorator import register_test_skill
from adviser_allocation.skills.executor import SkillExecutor, SkillResult

__all__ = [
    'Skill',
    'SkillRegistry',
    'register_test_skill',
    'SkillExecutor',
    'SkillResult',
]
