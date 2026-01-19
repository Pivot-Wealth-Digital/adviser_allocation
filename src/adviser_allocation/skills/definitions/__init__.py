"""Test skill definitions - register all test skills for the system."""

# Import all skill definition modules to trigger @test_skill decorators
from adviser_allocation.skills.definitions import unit_tests, integration_tests, e2e_tests

__all__ = ['unit_tests', 'integration_tests', 'e2e_tests']
