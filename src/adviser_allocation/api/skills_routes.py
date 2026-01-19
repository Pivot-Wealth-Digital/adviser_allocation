"""API routes for test skill discovery and execution."""

from flask import Blueprint, jsonify, request

from adviser_allocation.skills.executor import SkillExecutor
from adviser_allocation.skills.registry import SkillRegistry

skills_bp = Blueprint('skills', __name__, url_prefix='/api/skills')


@skills_bp.route('', methods=['GET'])
def list_skills():
    """Get all available test skills with optional filtering.

    Query Parameters:
        category (str, optional): Filter by category (unit, integration, e2e)
        tags (str, optional): Comma-separated tags to filter by (OR logic)
        required_only (bool, optional): If 'true', show only deployment-required skills

    Returns:
        JSON array of skill objects with metadata
    """
    category = request.args.get('category')
    tags = request.args.get('tags')
    required_only = request.args.get('required_only', 'false').lower() == 'true'

    # Parse tags if provided
    tag_list = [t.strip() for t in tags.split(',')] if tags else None

    # Get filtered skills
    if required_only:
        skills = SkillRegistry.get_required_skills()
    else:
        skills = SkillRegistry.list_skills(category=category, tags=tag_list)

    # Format response
    skills_data = [
        {
            'name': skill.name,
            'full_name': skill.full_name,
            'category': skill.category,
            'description': skill.description,
            'proficiency_level': skill.proficiency_level,
            'required_for_deployment': skill.required_for_deployment,
            'timeout_seconds': skill.timeout_seconds,
            'dependencies': skill.dependencies,
            'tags': skill.tags,
        }
        for skill in skills
    ]

    return jsonify({
        'total': len(skills_data),
        'skills': skills_data,
    })


@skills_bp.route('/<skill_name>', methods=['GET'])
def get_skill(skill_name):
    """Get details about a specific skill.

    Args:
        skill_name: Skill identifier or full name (e.g., 'allocation_logic' or 'unit/allocation_logic')

    Returns:
        Skill details as JSON, or 404 if not found
    """
    skill = SkillRegistry.get_skill(skill_name)

    if not skill:
        return jsonify({'error': f'Skill "{skill_name}" not found'}), 404

    return jsonify({
        'name': skill.name,
        'full_name': skill.full_name,
        'category': skill.category,
        'description': skill.description,
        'proficiency_level': skill.proficiency_level,
        'required_for_deployment': skill.required_for_deployment,
        'timeout_seconds': skill.timeout_seconds,
        'test_file_pattern': skill.test_file_pattern,
        'dependencies': skill.dependencies,
        'tags': skill.tags,
    })


@skills_bp.route('/status', methods=['GET'])
def skill_status():
    """Get overview of all registered skills and their categories.

    Returns:
        Summary statistics about registered skills
    """
    all_skills = SkillRegistry.get_all_skills()
    required_skills = SkillRegistry.get_required_skills()

    # Group by category
    by_category = {}
    for skill in all_skills:
        if skill.category not in by_category:
            by_category[skill.category] = []
        by_category[skill.category].append(skill.name)

    return jsonify({
        'total_skills': len(all_skills),
        'required_skills_count': len(required_skills),
        'by_category': by_category,
        'categories': sorted(by_category.keys()),
    })


@skills_bp.route('/<skill_name>/run', methods=['POST'])
def run_skill(skill_name):
    """Execute a specific test skill.

    Args:
        skill_name: Skill identifier or full name

    Returns:
        Execution result with status, duration, test count, and output
    """
    executor = SkillExecutor()

    try:
        result = executor.run_skill(skill_name)

        return jsonify({
            'skill_name': result.skill_name,
            'status': result.status,
            'passed': result.passed,
            'duration_seconds': result.duration_seconds,
            'test_count': result.test_count,
            'coverage_percent': result.coverage_percent,
            'output': result.output,
            'error_message': result.error_message,
        })

    except ValueError as e:
        return jsonify({'error': str(e)}), 404


@skills_bp.route('/run/required', methods=['POST'])
def run_required():
    """Execute all skills required for deployment.

    Returns:
        Array of execution results
    """
    executor = SkillExecutor()
    results = executor.run_all_required()

    # Format results
    results_data = [
        {
            'skill_name': r.skill_name,
            'status': r.status,
            'passed': r.passed,
            'duration_seconds': r.duration_seconds,
            'test_count': r.test_count,
            'coverage_percent': r.coverage_percent,
            'error_message': r.error_message,
        }
        for r in results
    ]

    all_passed = all(r.passed for r in results)

    return jsonify({
        'all_passed': all_passed,
        'total': len(results_data),
        'passed_count': sum(1 for r in results_data if r['passed']),
        'results': results_data,
    })


@skills_bp.route('/run/category/<category>', methods=['POST'])
def run_category(category):
    """Execute all skills in a category.

    Args:
        category: Category name (unit, integration, e2e)

    Returns:
        Array of execution results
    """
    executor = SkillExecutor()
    results = executor.run_by_category(category)

    if not results:
        return jsonify({'error': f'No skills found in category "{category}"'}), 404

    # Format results
    results_data = [
        {
            'skill_name': r.skill_name,
            'status': r.status,
            'passed': r.passed,
            'duration_seconds': r.duration_seconds,
            'test_count': r.test_count,
            'coverage_percent': r.coverage_percent,
            'error_message': r.error_message,
        }
        for r in results
    ]

    all_passed = all(r.passed for r in results)

    return jsonify({
        'category': category,
        'all_passed': all_passed,
        'total': len(results_data),
        'passed_count': sum(1 for r in results_data if r['passed']),
        'results': results_data,
    })


@skills_bp.route('/run/tags', methods=['POST'])
def run_tags():
    """Execute all skills matching given tags.

    JSON Body:
        tags (list): List of tags to filter by (OR logic)

    Returns:
        Array of execution results
    """
    data = request.get_json() or {}
    tags = data.get('tags', [])

    if not tags:
        return jsonify({'error': 'tags parameter is required'}), 400

    executor = SkillExecutor()
    results = executor.run_by_tags(tags)

    if not results:
        return jsonify({'error': f'No skills found with tags {tags}'}), 404

    # Format results
    results_data = [
        {
            'skill_name': r.skill_name,
            'status': r.status,
            'passed': r.passed,
            'duration_seconds': r.duration_seconds,
            'test_count': r.test_count,
            'coverage_percent': r.coverage_percent,
            'error_message': r.error_message,
        }
        for r in results
    ]

    all_passed = all(r.passed for r in results)

    return jsonify({
        'tags': tags,
        'all_passed': all_passed,
        'total': len(results_data),
        'passed_count': sum(1 for r in results_data if r['passed']),
        'results': results_data,
    })
