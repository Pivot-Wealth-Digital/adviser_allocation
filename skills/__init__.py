"""Test skill framework - centralized test discovery and execution."""

from skills.skill import Skill
from skills.registry import SkillRegistry
from skills.decorator import register_test_skill
from skills.executor import SkillExecutor, SkillResult

__all__ = [
    'Skill',
    'SkillRegistry',
    'register_test_skill',
    'SkillExecutor',
    'SkillResult',
]
