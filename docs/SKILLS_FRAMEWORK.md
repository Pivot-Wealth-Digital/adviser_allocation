# Test Skill Framework

A system-wide test discovery and execution framework that enables centralized management of test skills across the adviser allocation system.

## Overview

The test skill framework provides:

- **Centralized Discovery**: View all test skills via a registry
- **Flexible Execution**: Run tests by skill, category, tags, or deployment requirements
- **Metadata Rich**: Each skill includes description, proficiency level, dependencies, and performance data
- **API Integration**: RESTful endpoints for programmatic access
- **Scalable**: Registry pattern allows unlimited test skill definitions

## Architecture

```
┌─────────────────────────────────────────┐
│         Skill Framework Core            │
├─────────────────────────────────────────┤
│                                         │
│  ┌─────────────────────────────────┐   │
│  │  SkillRegistry (Singleton)      │   │
│  │  ├─ register(skill)             │   │
│  │  ├─ get_skill(name)             │   │
│  │  ├─ list_skills(filters)        │   │
│  │  └─ get_all_skills()            │   │
│  └─────────────────────────────────┘   │
│                                         │
│  ┌─────────────────────────────────┐   │
│  │  Skill (Dataclass)              │   │
│  │  ├─ name, category              │   │
│  │  ├─ description                 │   │
│  │  ├─ proficiency_level           │   │
│  │  ├─ required_for_deployment     │   │
│  │  ├─ dependencies                │   │
│  │  └─ tags                        │   │
│  └─────────────────────────────────┘   │
│                                         │
│  ┌─────────────────────────────────┐   │
│  │  SkillExecutor                  │   │
│  │  ├─ run_skill(name)             │   │
│  │  ├─ run_all_required()          │   │
│  │  ├─ run_by_category(cat)        │   │
│  │  └─ run_by_tags(tags)           │   │
│  └─────────────────────────────────┘   │
│                                         │
│  ┌─────────────────────────────────┐   │
│  │  @register_test_skill           │   │
│  │  Decorator for registration     │   │
│  └─────────────────────────────────┘   │
│                                         │
└─────────────────────────────────────────┘
```

## Core Components

### Skill (Dataclass)

Represents a test skill with metadata:

```python
from skills import Skill

skill = Skill(
    name="allocation_logic",
    category="unit",
    description="Unit tests for allocation algorithm",
    test_file_pattern="tests/test_allocation_logic.py",
    proficiency_level="advanced",
    timeout_seconds=300,
    required_for_deployment=True,
    dependencies=["unit/cache_utils"],
    tags=["core", "critical", "algorithm"]
)
```

**Attributes:**
- `name`: Skill identifier (e.g., "allocation_logic")
- `category`: Test category (unit, integration, e2e)
- `description`: Human-readable description
- `test_file_pattern`: pytest file pattern to execute
- `proficiency_level`: beginner, intermediate, advanced
- `timeout_seconds`: Max execution time (default: 300)
- `required_for_deployment`: Must pass before deployment (default: False)
- `dependencies`: List of skill names this depends on
- `tags`: Filtering tags (e.g., ["core", "critical"])

**Properties:**
- `full_name`: Returns "category/name" (e.g., "unit/allocation_logic")
- `identifier`: Returns just the name

### SkillRegistry (Singleton)

Thread-safe central registry for skill management:

```python
from skills import SkillRegistry

# Register a skill
SkillRegistry.register(skill)

# Get a skill by full name or identifier
skill = SkillRegistry.get_skill("allocation_logic")
skill = SkillRegistry.get_skill("unit/allocation_logic")

# List all skills
all_skills = SkillRegistry.get_all_skills()

# Filter skills
unit_tests = SkillRegistry.list_skills(category="unit")
critical = SkillRegistry.list_skills(tags=["critical"])

# Get deployment-required skills
required = SkillRegistry.get_required_skills()

# Get count
count = SkillRegistry.skill_count()
```

### @register_test_skill Decorator

Register a function as a test skill:

```python
from skills import register_test_skill

@register_test_skill(
    name="allocation_logic",
    category="unit",
    description="Unit tests for allocation algorithm",
    test_file_pattern="tests/test_allocation_logic.py",
    proficiency_level="advanced",
    required_for_deployment=True,
    tags=["core", "critical"]
)
def test_allocation_logic():
    """Run unit tests for allocation algorithm."""
    pass
```

The decorator registers the skill in the central registry and returns the original function unchanged.

### SkillExecutor

Execute skills and collect results:

```python
from skills import SkillExecutor

executor = SkillExecutor(verbose=True)

# Run a single skill
result = executor.run_skill("allocation_logic")

# Run all required skills
results = executor.run_all_required()

# Run all skills in a category
results = executor.run_by_category("unit")

# Run skills with specific tags
results = executor.run_by_tags(["core", "critical"])

# Run all skills
results = executor.run_all()

# Print human-readable results
executor.print_results(results)
```

**SkillResult attributes:**
- `skill_name`: Full skill name
- `passed`: Boolean indicating success
- `duration_seconds`: Execution time
- `test_count`: Number of tests run
- `coverage_percent`: Code coverage percentage
- `output`: Last 5000 characters of pytest output
- `error_message`: Error if execution failed
- `status`: "PASSED" or "FAILED" property

## Predefined Skills

All test skills are registered in `skills/definitions/`:

### Unit Tests

- `unit/allocation_logic` - Core allocation algorithm tests
- `unit/cache_utils` - Caching utilities tests
- `unit/firestore_helpers` - Firestore helper tests
- `unit/http_client` - HTTP client with retry tests
- `unit/oauth_service` - OAuth service tests
- `unit/all_unit_tests` - All unit tests aggregated

### Integration Tests

- `integration/app_e2e` - End-to-end Playwright tests
- `integration/all_integration_tests` - All integration tests

### E2E Tests

- `e2e/all_e2e_tests` - All E2E and system tests

All skills marked with `required_for_deployment: True` must pass before deployment.

## REST API

The framework provides REST endpoints at `/api/skills`:

### List Skills

```bash
GET /api/skills
GET /api/skills?category=unit
GET /api/skills?tags=critical,core
GET /api/skills?required_only=true
```

**Response:**
```json
{
  "total": 6,
  "skills": [
    {
      "name": "allocation_logic",
      "full_name": "unit/allocation_logic",
      "category": "unit",
      "description": "Unit tests for allocation algorithm",
      "proficiency_level": "advanced",
      "required_for_deployment": true,
      "timeout_seconds": 300,
      "dependencies": [],
      "tags": ["core", "critical"]
    }
  ]
}
```

### Get Skill Details

```bash
GET /api/skills/{skill_name}
GET /api/skills/allocation_logic
GET /api/skills/unit/allocation_logic
```

**Response:**
```json
{
  "name": "allocation_logic",
  "full_name": "unit/allocation_logic",
  "category": "unit",
  "description": "Unit tests for allocation algorithm",
  "proficiency_level": "advanced",
  "required_for_deployment": true,
  "timeout_seconds": 300,
  "test_file_pattern": "tests/test_allocation_logic.py",
  "dependencies": [],
  "tags": ["core", "critical"]
}
```

### Get Skills Overview

```bash
GET /api/skills/status
```

**Response:**
```json
{
  "total_skills": 11,
  "required_skills_count": 8,
  "categories": ["e2e", "integration", "unit"],
  "by_category": {
    "unit": ["allocation_logic", "cache_utils", ...],
    "integration": ["app_e2e", ...],
    "e2e": ["all_e2e_tests"]
  }
}
```

### Run a Skill

```bash
POST /api/skills/{skill_name}/run
POST /api/skills/allocation_logic/run
```

**Response:**
```json
{
  "skill_name": "unit/allocation_logic",
  "status": "PASSED",
  "passed": true,
  "duration_seconds": 2.45,
  "test_count": 12,
  "coverage_percent": 87.5,
  "output": "... last 5000 chars of pytest output ...",
  "error_message": null
}
```

### Run Required Skills

```bash
POST /api/skills/run/required
```

**Response:**
```json
{
  "all_passed": true,
  "total": 8,
  "passed_count": 8,
  "results": [
    {
      "skill_name": "unit/allocation_logic",
      "status": "PASSED",
      "passed": true,
      "duration_seconds": 2.45,
      ...
    }
  ]
}
```

### Run Skills by Category

```bash
POST /api/skills/run/category/unit
POST /api/skills/run/category/integration
```

**Response:**
```json
{
  "category": "unit",
  "all_passed": true,
  "total": 6,
  "passed_count": 6,
  "results": [...]
}
```

### Run Skills by Tags

```bash
POST /api/skills/run/tags
Content-Type: application/json

{
  "tags": ["critical", "core"]
}
```

**Response:**
```json
{
  "tags": ["critical", "core"],
  "all_passed": true,
  "total": 4,
  "passed_count": 4,
  "results": [...]
}
```

## Python API Usage

### Running Skills from Code

```python
from skills import SkillExecutor, SkillRegistry

# List all skills
all_skills = SkillRegistry.get_all_skills()
for skill in all_skills:
    print(f"{skill.full_name}: {skill.description}")

# Run required skills
executor = SkillExecutor(verbose=True)
results = executor.run_all_required()

if all(r.passed for r in results):
    print("All required skills passed!")
else:
    print("Some skills failed:")
    for r in results:
        if not r.passed:
            print(f"  - {r.skill_name}: {r.error_message}")
```

### Creating New Skills

1. Create a test file in `tests/`:

```python
# tests/test_my_feature.py
def test_feature_works():
    assert True
```

2. Define a skill in `skills/definitions/` or a custom module:

```python
from skills import register_test_skill

@register_test_skill(
    name="my_feature",
    category="unit",
    description="Tests for my feature",
    test_file_pattern="tests/test_my_feature.py",
    tags=["new"],
    required_for_deployment=False
)
def test_my_feature():
    """Run tests for my feature."""
    pass
```

3. Import the skill definition in your module or in `main.py`:

```python
import skills.definitions
# or
from my_module import test_my_feature
```

4. Access via registry:

```python
from skills import SkillRegistry
skill = SkillRegistry.get_skill("my_feature")
```

## Cloud Build Integration

The skill framework integrates with Cloud Build for automated testing:

```yaml
steps:
  # ... setup steps ...

  - name: 'python'
    entrypoint: 'python'
    args: [
      '-c',
      'from skills.executor import SkillExecutor;
       results = SkillExecutor().run_all_required();
       exit(0 if all(r.passed for r in results) else 1)'
    ]

    # Or use specific category:
    # args: ['-m', 'pytest', 'tests/test_unit_*.py']
```

Current `cloudbuild.yaml` already runs all tests. The skill framework enhances visibility and control over which tests block deployment.

## Best Practices

### 1. Naming Conventions

- Skill names: `snake_case` (e.g., "allocation_logic")
- Categories: one of unit, integration, e2e
- Tags: lowercase, hyphen-separated (e.g., "user-facing", "performance-critical")

### 2. Proficiency Levels

- **beginner**: Skill can be executed and understood without deep system knowledge
- **intermediate**: Requires understanding of feature area (default)
- **advanced**: Requires deep knowledge of system internals or complex workflows

### 3. Dependencies

Use for skills that must run in a specific order or that depend on setup:

```python
@register_test_skill(
    name="authentication",
    category="integration",
    description="Full authentication flow tests",
    test_file_pattern="tests/test_auth.py",
    dependencies=["unit/oauth_service"],  # Run unit tests first
)
```

### 4. Tags for Organization

Use tags to group related skills for batch execution:

```python
# In skill definitions:
tags=["core", "critical"]      # Essential for system stability
tags=["performance"]           # Performance-related tests
tags=["security"]              # Security-related tests
tags=["user-facing"]           # Tests user-visible functionality
tags=["optional"]              # Non-blocking tests
```

Then run via API:

```bash
POST /api/skills/run/tags
{"tags": ["security"]}
```

### 5. Timeout Configuration

Set realistic timeouts for your skill:

```python
@register_test_skill(
    name="slow_integration_test",
    category="integration",
    test_file_pattern="tests/test_slow.py",
    timeout_seconds=600,  # 10 minutes for slow tests
)
```

### 6. Required Skills

Mark skills as `required_for_deployment=True` only if they're critical:

```python
# Only set this for tests that MUST pass
required_for_deployment=True,

# Leave False for optional tests
required_for_deployment=False,
```

## Testing the Framework

The skill framework itself has 33 unit tests in `tests/test_skills.py`:

```bash
# Run skill framework tests
python3 -m pytest tests/test_skills.py -v

# Run all tests including skills
python3 -m pytest tests/ -v
```

Test coverage includes:
- Skill metadata and properties
- Registry singleton pattern
- Skill registration and retrieval
- Filtering by category and tags
- Decorator functionality
- Executor initialization and execution
- Result tracking and formatting

## Troubleshooting

### Skill Not Found

```python
from skills import SkillRegistry

# Try both formats
skill = SkillRegistry.get_skill("allocation_logic")
skill = SkillRegistry.get_skill("unit/allocation_logic")

if not skill:
    print("Skill not found. Available skills:")
    for s in SkillRegistry.get_all_skills():
        print(f"  - {s.full_name}")
```

### Execution Timeout

If a skill times out, increase the timeout:

```python
@register_test_skill(
    name="slow_test",
    category="integration",
    test_file_pattern="tests/test_slow.py",
    timeout_seconds=900,  # Increased from default 300
)
```

### Missing Test Output

The executor captures the last 5000 characters of pytest output. For full output, run locally:

```bash
pytest tests/test_my_feature.py -v
```

## Performance

- **Registry Initialization**: ~1ms (singleton pattern)
- **Skill Lookup**: O(1) for identifier, O(n) for filtering
- **Execution Overhead**: <1% (just pytest wrapper)
- **Memory**: ~1KB per skill registered

The framework scales to hundreds of test skills with negligible overhead.

## Future Enhancements

Potential improvements for future versions:

1. **Skill Dependencies Resolution**: Auto-run dependent skills
2. **Metrics Persistence**: Store execution metrics in Firestore
3. **Dashboard UI**: Web interface for skill discovery and execution
4. **Test History**: Track skill execution trends over time
5. **Smart Suggestions**: Recommend which skills to run based on code changes
6. **Parallel Execution**: Run independent skills concurrently
7. **Skill Profiling**: Track which skills are slowest
8. **Integration Hooks**: Run skills on git events (push, PR, etc.)

## Summary

The test skill framework provides a powerful, extensible system for managing tests across your codebase. It supports:

- ✅ Centralized test discovery and execution
- ✅ Flexible filtering by category, tags, and requirements
- ✅ Rich metadata about each test skill
- ✅ REST API for programmatic access
- ✅ Scalable registry pattern
- ✅ Thread-safe singleton implementation
- ✅ Comprehensive test coverage (33 tests)

Start using skills in your CI/CD pipeline and Python code for better test organization and visibility!
