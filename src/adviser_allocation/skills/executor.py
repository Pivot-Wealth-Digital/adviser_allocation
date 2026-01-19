"""Skill executor - runs tests and collects metrics."""

import json
import subprocess
import time
from dataclasses import dataclass
from typing import List, Optional

from skills.registry import SkillRegistry


@dataclass
class SkillResult:
    """Result of executing a test skill."""

    skill_name: str
    """Full skill name (category/name)"""

    passed: bool
    """Whether all tests passed"""

    duration_seconds: float
    """Execution time in seconds"""

    test_count: int = 0
    """Number of tests executed"""

    coverage_percent: Optional[float] = None
    """Code coverage percentage (if measured)"""

    output: str = ""
    """Last 5000 characters of pytest output"""

    error_message: Optional[str] = None
    """Error message if execution failed"""

    @property
    def status(self) -> str:
        """Return human-readable status."""
        return "PASSED" if self.passed else "FAILED"


class SkillExecutor:
    """Executes test skills and collects results.

    Uses pytest to run tests defined in skill patterns.
    Supports running individual skills, skill suites, or all required skills.
    """

    def __init__(self, verbose: bool = False):
        """Initialize executor.

        Args:
            verbose: If True, print detailed output during execution
        """
        self.verbose = verbose

    def run_skill(self, skill_name: str) -> SkillResult:
        """Execute a single test skill.

        Args:
            skill_name: Skill identifier or full name

        Returns:
            SkillResult with execution results

        Raises:
            ValueError: If skill not found
        """
        skill = SkillRegistry.get_skill(skill_name)
        if not skill:
            raise ValueError(f"Skill '{skill_name}' not found in registry")

        return self._execute_pytest(skill)

    def run_all_required(self) -> List[SkillResult]:
        """Execute all skills required for deployment.

        Returns:
            List of SkillResult for each required skill
        """
        required_skills = SkillRegistry.get_required_skills()
        results = []

        for skill in required_skills:
            result = self._execute_pytest(skill)
            results.append(result)

        return results

    def run_by_category(self, category: str) -> List[SkillResult]:
        """Execute all skills in a category.

        Args:
            category: Category name (e.g., 'unit', 'e2e')

        Returns:
            List of SkillResult for each skill in category
        """
        skills = SkillRegistry.list_skills(category=category)
        results = []

        for skill in skills:
            result = self._execute_pytest(skill)
            results.append(result)

        return results

    def run_by_tags(self, tags: List[str]) -> List[SkillResult]:
        """Execute all skills matching ANY of the given tags.

        Args:
            tags: List of tag filters

        Returns:
            List of SkillResult for matching skills
        """
        skills = SkillRegistry.list_skills(tags=tags)
        results = []

        for skill in skills:
            result = self._execute_pytest(skill)
            results.append(result)

        return results

    def run_all(self) -> List[SkillResult]:
        """Execute all registered skills.

        Returns:
            List of SkillResult for each skill
        """
        skills = SkillRegistry.get_all_skills()
        results = []

        for skill in skills:
            result = self._execute_pytest(skill)
            results.append(result)

        return results

    def _execute_pytest(self, skill) -> SkillResult:
        """Execute pytest for a skill and collect results.

        Args:
            skill: TestSkill instance

        Returns:
            SkillResult with execution data
        """
        start_time = time.time()

        # Build pytest command
        cmd = [
            "pytest",
            skill.test_file_pattern,
            "-v",
            "--tb=short",
            f"--timeout={skill.timeout_seconds}",
            "--json-report",
            "--json-report-file=/tmp/report.json",
            "--cov=.",
            "--cov-report=json:/tmp/coverage.json",
        ]

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=skill.timeout_seconds + 30,  # Add buffer for cleanup
            )

            duration = time.time() - start_time
            passed = result.returncode == 0
            output = (result.stdout + "\n" + result.stderr)[-5000:]

            # Try to extract coverage
            coverage_percent = self._extract_coverage()

            # Try to extract test count
            test_count = self._extract_test_count(output)

            return SkillResult(
                skill_name=skill.full_name,
                passed=passed,
                duration_seconds=duration,
                test_count=test_count,
                coverage_percent=coverage_percent,
                output=output,
                error_message=None if passed else "Tests failed",
            )

        except subprocess.TimeoutExpired:
            duration = time.time() - start_time
            return SkillResult(
                skill_name=skill.full_name,
                passed=False,
                duration_seconds=duration,
                output="",
                error_message=f"Execution timed out after {skill.timeout_seconds}s",
            )

        except Exception as e:
            duration = time.time() - start_time
            return SkillResult(
                skill_name=skill.full_name,
                passed=False,
                duration_seconds=duration,
                output="",
                error_message=f"Execution error: {str(e)}",
            )

    @staticmethod
    def _extract_coverage() -> Optional[float]:
        """Extract coverage percentage from coverage.json report."""
        try:
            with open("/tmp/coverage.json", "r") as f:
                data = json.load(f)
                if "totals" in data and "percent_covered" in data["totals"]:
                    return data["totals"]["percent_covered"]
        except Exception:
            pass
        return None

    @staticmethod
    def _extract_test_count(output: str) -> int:
        """Extract test count from pytest output."""
        # Look for "X passed" pattern
        import re

        match = re.search(r"(\d+) passed", output)
        if match:
            return int(match.group(1))
        return 0

    def print_results(self, results: List[SkillResult]) -> None:
        """Print human-readable results summary.

        Args:
            results: List of SkillResult to display
        """
        print("\n" + "=" * 80)
        print("TEST SKILL EXECUTION RESULTS")
        print("=" * 80)

        for result in results:
            status_icon = "✓" if result.passed else "✗"
            coverage_str = (
                f" [{result.coverage_percent:.1f}%]"
                if result.coverage_percent
                else ""
            )
            print(
                f"{status_icon} {result.skill_name:40s} "
                f"{result.duration_seconds:6.2f}s "
                f"{coverage_str}"
            )
            if result.error_message:
                print(f"  Error: {result.error_message}")

        print("=" * 80)

        # Summary
        passed = sum(1 for r in results if r.passed)
        total = len(results)
        print(f"Summary: {passed}/{total} skills passed")

        if passed < total:
            print("\nFailed skills:")
            for result in results:
                if not result.passed:
                    print(f"  - {result.skill_name}")
                    if result.error_message:
                        print(f"    {result.error_message}")
