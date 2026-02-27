"""Unit tests for the test skill framework."""

import pytest
from adviser_allocation.skills.decorator import register_test_skill
from adviser_allocation.skills.executor import SkillExecutor, SkillResult
from adviser_allocation.skills.registry import SkillRegistry
from adviser_allocation.skills.skill import Skill


class TestSkillDataclass:
    """Tests for Skill dataclass."""

    def test_skill_initialization(self):
        """Test basic skill creation."""
        skill = Skill(
            name="test_skill",
            category="unit",
            description="A test skill",
            test_file_pattern="tests/test_*.py",
        )

        assert skill.name == "test_skill"
        assert skill.category == "unit"
        assert skill.description == "A test skill"
        assert skill.test_file_pattern == "tests/test_*.py"

    def test_skill_full_name(self):
        """Test full_name property."""
        skill = Skill(
            name="my_skill",
            category="integration",
            description="Test",
            test_file_pattern="tests/test_*.py",
        )

        assert skill.full_name == "integration/my_skill"

    def test_skill_identifier(self):
        """Test identifier property."""
        skill = Skill(
            name="test_identifier",
            category="unit",
            description="Test",
            test_file_pattern="tests/test_*.py",
        )

        assert skill.identifier == "test_identifier"

    def test_skill_default_values(self):
        """Test default values are set correctly."""
        skill = Skill(
            name="test",
            category="unit",
            description="Test",
            test_file_pattern="tests/test_*.py",
        )

        assert skill.proficiency_level == "intermediate"
        assert skill.timeout_seconds == 300
        assert skill.required_for_deployment is False
        assert skill.dependencies == []
        assert skill.tags == []

    def test_skill_with_custom_values(self):
        """Test skill creation with custom values."""
        skill = Skill(
            name="critical_test",
            category="unit",
            description="Critical test",
            test_file_pattern="tests/critical/*.py",
            proficiency_level="advanced",
            timeout_seconds=600,
            required_for_deployment=True,
            dependencies=["unit/basic_test"],
            tags=["core", "critical"],
        )

        assert skill.proficiency_level == "advanced"
        assert skill.timeout_seconds == 600
        assert skill.required_for_deployment is True
        assert skill.dependencies == ["unit/basic_test"]
        assert skill.tags == ["core", "critical"]


class TestSkillRegistry:
    """Tests for SkillRegistry."""

    def setup_method(self):
        """Clear registry before each test."""
        SkillRegistry.clear()

    def test_registry_singleton(self):
        """Test registry is a singleton."""
        registry1 = SkillRegistry()
        registry2 = SkillRegistry()

        assert registry1 is registry2

    def test_register_skill(self):
        """Test registering a skill."""
        skill = Skill(
            name="test_skill",
            category="unit",
            description="Test",
            test_file_pattern="tests/test_*.py",
        )

        SkillRegistry.register(skill)

        assert SkillRegistry.skill_count() == 1

    def test_register_duplicate_skill_raises_error(self):
        """Test that registering duplicate skill raises error."""
        skill1 = Skill(
            name="test_skill",
            category="unit",
            description="Test",
            test_file_pattern="tests/test_*.py",
        )
        skill2 = Skill(
            name="test_skill",
            category="unit",
            description="Test 2",
            test_file_pattern="tests/test_*.py",
        )

        SkillRegistry.register(skill1)

        with pytest.raises(ValueError, match="already registered"):
            SkillRegistry.register(skill2)

    def test_get_skill_by_full_name(self):
        """Test retrieving skill by full name."""
        skill = Skill(
            name="my_skill",
            category="unit",
            description="Test",
            test_file_pattern="tests/test_*.py",
        )

        SkillRegistry.register(skill)
        retrieved = SkillRegistry.get_skill("unit/my_skill")

        assert retrieved is skill

    def test_get_skill_by_identifier(self):
        """Test retrieving skill by identifier."""
        skill = Skill(
            name="my_skill",
            category="unit",
            description="Test",
            test_file_pattern="tests/test_*.py",
        )

        SkillRegistry.register(skill)
        retrieved = SkillRegistry.get_skill("my_skill")

        assert retrieved is skill

    def test_get_nonexistent_skill_returns_none(self):
        """Test retrieving nonexistent skill returns None."""
        retrieved = SkillRegistry.get_skill("nonexistent")

        assert retrieved is None

    def test_list_all_skills(self):
        """Test listing all skills."""
        skill1 = Skill(
            name="skill1",
            category="unit",
            description="Test 1",
            test_file_pattern="tests/test_1.py",
        )
        skill2 = Skill(
            name="skill2",
            category="integration",
            description="Test 2",
            test_file_pattern="tests/test_2.py",
        )

        SkillRegistry.register(skill1)
        SkillRegistry.register(skill2)
        skills = SkillRegistry.list_skills()

        assert len(skills) == 2
        assert skill1 in skills
        assert skill2 in skills

    def test_list_skills_by_category(self):
        """Test filtering skills by category."""
        skill1 = Skill(
            name="skill1",
            category="unit",
            description="Test 1",
            test_file_pattern="tests/test_1.py",
        )
        skill2 = Skill(
            name="skill2",
            category="integration",
            description="Test 2",
            test_file_pattern="tests/test_2.py",
        )

        SkillRegistry.register(skill1)
        SkillRegistry.register(skill2)
        unit_skills = SkillRegistry.list_skills(category="unit")

        assert len(unit_skills) == 1
        assert unit_skills[0] is skill1

    def test_list_skills_by_tags(self):
        """Test filtering skills by tags."""
        skill1 = Skill(
            name="skill1",
            category="unit",
            description="Test 1",
            test_file_pattern="tests/test_1.py",
            tags=["critical", "core"],
        )
        skill2 = Skill(
            name="skill2",
            category="unit",
            description="Test 2",
            test_file_pattern="tests/test_2.py",
            tags=["optional"],
        )

        SkillRegistry.register(skill1)
        SkillRegistry.register(skill2)
        critical_skills = SkillRegistry.list_skills(tags=["critical"])

        assert len(critical_skills) == 1
        assert critical_skills[0] is skill1

    def test_list_skills_by_multiple_tags(self):
        """Test filtering skills by multiple tags (OR logic)."""
        skill1 = Skill(
            name="skill1",
            category="unit",
            description="Test 1",
            test_file_pattern="tests/test_1.py",
            tags=["critical"],
        )
        skill2 = Skill(
            name="skill2",
            category="unit",
            description="Test 2",
            test_file_pattern="tests/test_2.py",
            tags=["core"],
        )
        skill3 = Skill(
            name="skill3",
            category="unit",
            description="Test 3",
            test_file_pattern="tests/test_3.py",
            tags=["optional"],
        )

        SkillRegistry.register(skill1)
        SkillRegistry.register(skill2)
        SkillRegistry.register(skill3)
        important_skills = SkillRegistry.list_skills(tags=["critical", "core"])

        assert len(important_skills) == 2
        assert skill1 in important_skills
        assert skill2 in important_skills

    def test_get_required_skills(self):
        """Test getting only required skills."""
        skill1 = Skill(
            name="required_skill",
            category="unit",
            description="Required",
            test_file_pattern="tests/test_1.py",
            required_for_deployment=True,
        )
        skill2 = Skill(
            name="optional_skill",
            category="unit",
            description="Optional",
            test_file_pattern="tests/test_2.py",
            required_for_deployment=False,
        )

        SkillRegistry.register(skill1)
        SkillRegistry.register(skill2)
        required = SkillRegistry.get_required_skills()

        assert len(required) == 1
        assert required[0] is skill1

    def test_get_all_skills(self):
        """Test getting all skills."""
        skill1 = Skill(
            name="skill1",
            category="unit",
            description="Test 1",
            test_file_pattern="tests/test_1.py",
        )
        skill2 = Skill(
            name="skill2",
            category="integration",
            description="Test 2",
            test_file_pattern="tests/test_2.py",
        )

        SkillRegistry.register(skill1)
        SkillRegistry.register(skill2)
        all_skills = SkillRegistry.get_all_skills()

        assert len(all_skills) == 2

    def test_skill_count(self):
        """Test skill count."""
        assert SkillRegistry.skill_count() == 0

        skill = Skill(
            name="skill1",
            category="unit",
            description="Test",
            test_file_pattern="tests/test_1.py",
        )
        SkillRegistry.register(skill)

        assert SkillRegistry.skill_count() == 1


class DecoratorTests:
    """Tests for test_skill decorator."""

    def setup_method(self):
        """Clear registry before each test."""
        SkillRegistry.clear()

    def test_decorator_registers_skill(self):
        """Test that decorator registers a skill."""

        @register_test_skill(
            name="decorated_test",
            category="unit",
            description="A decorated test skill",
            test_file_pattern="tests/test_decorated.py",
        )
        def my_test_function():
            return "test"

        assert SkillRegistry.skill_count() == 1
        skill = SkillRegistry.get_skill("decorated_test")
        assert skill is not None
        assert skill.name == "decorated_test"

    def test_decorator_preserves_function(self):
        """Test that decorator returns original function."""

        @register_test_skill(
            name="test",
            category="unit",
            description="Test",
            test_file_pattern="tests/test.py",
        )
        def my_function():
            return "result"

        assert my_function() == "result"

    def test_decorator_with_all_parameters(self):
        """Test decorator with all optional parameters."""

        @register_test_skill(
            name="full_test",
            category="integration",
            description="Full test",
            test_file_pattern="tests/test_full.py",
            proficiency_level="advanced",
            timeout_seconds=600,
            required_for_deployment=True,
            dependencies=["unit/basic"],
            tags=["core", "critical"],
        )
        def full_test():
            pass

        skill = SkillRegistry.get_skill("full_test")
        assert skill.proficiency_level == "advanced"
        assert skill.timeout_seconds == 600
        assert skill.required_for_deployment is True
        assert skill.dependencies == ["unit/basic"]
        assert skill.tags == ["core", "critical"]


class TestSkillResult:
    """Tests for SkillResult dataclass."""

    def test_result_initialization(self):
        """Test basic result creation."""
        result = SkillResult(
            skill_name="test/skill",
            passed=True,
            duration_seconds=1.5,
        )

        assert result.skill_name == "test/skill"
        assert result.passed is True
        assert result.duration_seconds == 1.5

    def test_result_status_passed(self):
        """Test status property for passed test."""
        result = SkillResult(
            skill_name="test/skill",
            passed=True,
            duration_seconds=1.0,
        )

        assert result.status == "PASSED"

    def test_result_status_failed(self):
        """Test status property for failed test."""
        result = SkillResult(
            skill_name="test/skill",
            passed=False,
            duration_seconds=1.0,
        )

        assert result.status == "FAILED"

    def test_result_with_coverage(self):
        """Test result with coverage data."""
        result = SkillResult(
            skill_name="test/skill",
            passed=True,
            duration_seconds=1.0,
            coverage_percent=85.5,
        )

        assert result.coverage_percent == 85.5

    def test_result_with_error(self):
        """Test result with error message."""
        result = SkillResult(
            skill_name="test/skill",
            passed=False,
            duration_seconds=1.0,
            error_message="Connection timeout",
        )

        assert result.error_message == "Connection timeout"


class TestSkillExecutor:
    """Tests for SkillExecutor."""

    def setup_method(self):
        """Clear registry and setup test skills."""
        SkillRegistry.clear()

    def test_executor_initialization(self):
        """Test executor creation."""
        executor = SkillExecutor(verbose=True)

        assert executor.verbose is True

    def test_executor_initialization_defaults(self):
        """Test executor default values."""
        executor = SkillExecutor()

        assert executor.verbose is False

    def test_run_skill_not_found_raises_error(self):
        """Test running nonexistent skill raises error."""
        executor = SkillExecutor()

        with pytest.raises(ValueError, match="not found"):
            executor.run_skill("nonexistent")

    def test_run_by_category_empty_result(self):
        """Test running by category with no matching skills."""
        executor = SkillExecutor()
        results = executor.run_by_category("nonexistent")

        assert results == []

    def test_run_by_tags_empty_result(self):
        """Test running by tags with no matching skills."""
        executor = SkillExecutor()
        results = executor.run_by_tags(["nonexistent"])

        assert results == []

    def test_run_all_empty_registry(self):
        """Test running all when no skills registered."""
        executor = SkillExecutor()
        results = executor.run_all()

        assert results == []

    def test_run_all_required_empty_registry(self):
        """Test running required when no skills registered."""
        executor = SkillExecutor()
        results = executor.run_all_required()

        assert results == []

    def test_print_results_passed_skills(self, capsys):
        """Test printing results for passed skills."""
        result = SkillResult(
            skill_name="unit/test",
            passed=True,
            duration_seconds=1.5,
            test_count=10,
        )

        executor = SkillExecutor()
        executor.print_results([result])

        captured = capsys.readouterr()
        assert "unit/test" in captured.out
        assert "PASSED" in captured.out or "✓" in captured.out

    def test_print_results_failed_skills(self, capsys):
        """Test printing results for failed skills."""
        result = SkillResult(
            skill_name="unit/test",
            passed=False,
            duration_seconds=1.5,
            error_message="Assertion failed",
        )

        executor = SkillExecutor()
        executor.print_results([result])

        captured = capsys.readouterr()
        assert "unit/test" in captured.out
        assert "Assertion failed" in captured.out

    def test_print_results_multiple(self, capsys):
        """Test printing results for multiple skills."""
        results = [
            SkillResult(
                skill_name="unit/test1",
                passed=True,
                duration_seconds=1.0,
            ),
            SkillResult(
                skill_name="unit/test2",
                passed=False,
                duration_seconds=2.0,
                error_message="Failed",
            ),
        ]

        executor = SkillExecutor()
        executor.print_results(results)

        captured = capsys.readouterr()
        assert "1/2 skills passed" in captured.out
