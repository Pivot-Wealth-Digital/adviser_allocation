# Contributing Guide

## Development Workflow

Follow this workflow when making changes:

### 1. Create Feature Branch

```bash
git checkout -b feature/your-feature-name
# or
git checkout -b fix/your-bug-fix-name
```

### 2. Make Changes

- Write code in your feature branch
- Follow existing code style and patterns
- Add tests for new functionality

### 3. Run Tests Locally

```bash
# Run all tests
pytest tests/ -v

# Run with coverage
pytest tests/ -v --cov=. --cov-report=term-missing

# Run specific test file
pytest tests/test_allocation.py -v
```

**Requirement:** All 65 tests must pass before pushing

### 4. Commit with Clear Messages

```bash
git commit -m "Brief description of change

Longer description explaining why this change was made.
Reference any related issues or tickets.

Co-Authored-By: Your Name <your.email@example.com>"
```

### 5. Push to GitHub

```bash
git push origin feature/your-feature-name
```

### 6. Create Pull Request (Optional)

If using team workflow:
1. Go to GitHub repository
2. Create Pull Request from feature branch to `main`
3. Describe changes and link related issues
4. Request review from team

### 7. Cloud Build Auto-Deploys

Once merged to `main`:
1. Cloud Build automatically triggers
2. Runs all 65 tests
3. If all pass → Deploy to App Engine
4. If any fail → Block deployment, notify

---

## Critical Infrastructure to Preserve

⚠️ **Do NOT remove or rename these files:**
- `core/allocation.py` - Allocation algorithm
- `services/oauth_service.py` - OAuth token management
- `middleware/rate_limiter.py` - API rate limiting

⚠️ **Do NOT modify without testing:**
- `app.yaml` - App Engine configuration
- `requirements.txt` - Python dependencies
- Firestore collections and schema

---

## Common Change Scenarios

### Add New Feature

1. Create feature branch: `git checkout -b feature/new-feature`
2. Implement feature with tests
3. Ensure 65 tests pass: `pytest tests/ -v`
4. Commit and push: `git push origin feature/new-feature`
5. Merge to main (auto-deploys)

**Example: Add new endpoint**
1. Add route in `main.py` or appropriate route file
2. Implement logic in service module
3. Add tests in `tests/test_routes.py`
4. Update [API_REFERENCE.md](API_REFERENCE.md) with new endpoint

### Fix Bug

1. Identify bug and create test that reproduces it
2. Fix code to make test pass
3. Ensure all 65 tests pass
4. Commit: `git commit -m "Fix: description of bug fix"`
5. Push and deploy

### Update Dependencies

```bash
# Update requirements.txt with new version
# Test thoroughly!
pip install -r requirements.txt
pytest tests/ -v --cov=.

# Commit and push for deployment
git commit -m "Update: upgrade dependency X to version Y"
git push origin feature/update-deps
```

**Important:** Always test after dependency updates - breaking changes can occur

### Add New Firestore Collection

1. Create collection in Firestore Console (or via admin UI)
2. Add helper function in `utils/firestore_helpers.py`
3. Write tests for new collection operations
4. Document collection schema in [INFRASTRUCTURE.md](INFRASTRUCTURE.md)
5. Add CRUD endpoints as needed

### Change API Endpoint

1. Update endpoint URL in code
2. Update any HubSpot workflows that call it
3. Test with curl or Postman
4. Update [API_REFERENCE.md](API_REFERENCE.md)
5. Test end-to-end with real HubSpot data
6. Commit and deploy

### Update OAuth Flow

1. Test with real Employment Hero account
2. Verify tokens still work after changes
3. Check token refresh logic
4. Update [CONFIGURATION.md](CONFIGURATION.md) if needed
5. Test both local and production flows

### Add New Secret

1. Create secret in Google Secret Manager
2. Reference in code via `get_secret()`
3. Document in [CONFIGURATION.md](CONFIGURATION.md)
4. Update `app.yaml` if needed
5. Test in both local and production

### Modify Documentation

1. Update relevant markdown file
2. Check links still work
3. Verify formatting renders correctly in GitHub
4. Commit: `git commit -m "Docs: update documentation for X"`

---

## Deployment Process

The deployment is **automatic** via Cloud Build:

```
Git Push to main
  ↓
Cloud Build Triggered
  ↓
├─ Restore pip cache
├─ Install dependencies
├─ Run 65 tests
├─ Generate coverage report
│
├─ If ALL PASS:
│  └─ Deploy to App Engine ✅
│
└─ If ANY FAIL:
   └─ Block deployment ❌
      Notify via email
```

**Check deployment status:**

```bash
# List recent builds
gcloud builds list --project=pivot-digital-466902 --limit=5

# View build details
gcloud builds log BUILD_ID --project=pivot-digital-466902 --stream

# View build logs
gcloud builds log BUILD_ID --project=pivot-digital-466902
```

**Typical timeline:**
- Test run: 1-2 minutes
- Deployment: ~1 minute if tests pass
- Total: 2-3 minutes

---

## Emergency Fixes

If production breaks after deployment:

### 1. Check What's Wrong

```bash
# View current version
gcloud app versions list --project=pivot-digital-466902

# Check logs for errors
gcloud app logs tail -s default --project=pivot-digital-466902
```

### 2. Identify the Problem

- Review recent commits: `git log --oneline -10`
- Check which deployment failed
- View Cloud Build logs

### 3. Option A: Rollback to Previous Version

```bash
# Find previous stable version
gcloud app versions list --project=pivot-digital-466902

# Route traffic to previous version
gcloud app services set-traffic default \
  --splits=OLD_VERSION=1.0 \
  --project=pivot-digital-466902
```

### 4. Option B: Fix and Re-Deploy

```bash
# Fix the code
# (make changes, test locally)

# Revert bad commit OR create new fix commit
git revert BAD_COMMIT
# or
git commit -m "Fix: emergency patch for production issue"

# Push to trigger redeploy
git push origin main
# Cloud Build auto-deploys once tests pass
```

### 5. Monitor Deployment

```bash
# Watch new build
gcloud builds list --project=pivot-digital-466902 --limit=1 --follow
```

---

## Testing Requirements

### Test Coverage

- **Minimum:** 70% code coverage
- **Target:** 85%+ code coverage
- **Current:** 100% (all 65 tests passing)

```bash
# Run with coverage report
pytest tests/ --cov=. --cov-report=term-missing
```

### Test Types

**Unit Tests:**
- Test individual functions/methods
- Mock external dependencies
- Fast execution

**Integration Tests:**
- Test component interactions
- Use real Firestore (local emulator preferred)
- Verify API integrations

**End-to-End Tests:**
- Test full workflows
- Use Playwright for UI testing
- Verify real-world scenarios

### Writing Tests

Example test structure:

```python
def test_allocation_finds_earliest_adviser(mock_firestore):
    """Test allocation algorithm finds adviser with earliest availability."""
    # Setup
    advisers = [
        {"email": "john@example.com", "capacity": 5},
        {"email": "jane@example.com", "capacity": 3},
    ]
    
    # Execute
    result = get_adviser(advisers, service_package="Series A")
    
    # Assert
    assert result["email"] == "jane@example.com"
```

### Pre-Commit Checklist

Before pushing, ensure:
- [ ] All 65 tests pass: `pytest tests/ -v`
- [ ] No linting errors: `flake8 .` (if enabled)
- [ ] Coverage maintained: `pytest --cov=. --cov-report=term-missing`
- [ ] Code follows style guide
- [ ] Documentation updated if needed
- [ ] New endpoints documented in API_REFERENCE.md

---

## Code Style Guide

### Python

- Follow PEP 8
- Use type hints where possible
- Maximum line length: 100 characters (unless URL/string)
- Use meaningful variable names
- Add docstrings to functions

Example:
```python
def calculate_adviser_capacity(
    adviser_email: str,
    week_start: date,
    include_overrides: bool = True
) -> int:
    """Calculate available capacity for adviser in given week.
    
    Args:
        adviser_email: Email of adviser
        week_start: Start date of week
        include_overrides: Whether to apply capacity overrides
        
    Returns:
        Available client slots for week
    """
    # implementation
    pass
```

### HTML/JavaScript

- Use consistent indentation (2 spaces)
- Use semantic HTML
- Add comments for complex logic
- Use meaningful CSS class names

### Documentation

- Use clear, concise language
- Include code examples where helpful
- Link to related documentation
- Keep lines under 100 characters

---

## Adding Documentation

When you make changes, update:

1. **README.md** - If user-facing changes
   - Update feature descriptions
   - Update configuration options
   - Update deployment instructions

2. **API_REFERENCE.md** - If adding/changing endpoints
   - Document endpoint purpose
   - Include request/response examples
   - List query parameters and headers

3. **CONFIGURATION.md** - If new environment variables
   - Document new variables
   - Include setup instructions
   - Explain configuration steps

4. **ARCHITECTURE.md** - If changing core system
   - Update architecture diagrams
   - Document algorithm changes
   - Explain new modules

5. **In-code comments** - For complex logic
   - Explain the "why", not the "what"
   - Keep comments minimal
   - Update if logic changes

### Documentation Checklist

- [ ] Relevant markdown files updated
- [ ] Links still work
- [ ] Code examples tested
- [ ] Formatting renders correctly
- [ ] No typos or grammar errors

---

## What NOT to Do

❌ **Don't commit secrets**
- Use Secret Manager instead
- Never store credentials in code
- Never commit `.env` files

❌ **Don't skip tests**
- 65 tests must pass before deployment
- Broken tests block deployment automatically
- Always run full test suite locally

❌ **Don't modify `app.yaml` region**
- Region is locked to `australia-southeast1`
- Changing causes deployment failure
- Contact DevOps if region change needed

❌ **Don't force-push to main**
- Will auto-deploy broken code
- Always use feature branches
- Merge via normal flow

❌ **Don't delete Firestore collections**
- Data loss is permanent and irreversible
- Always backup before deleting
- Coordinate with team

❌ **Don't rename critical files**
- `core/allocation.py` - allocation algorithm
- `services/oauth_service.py` - OAuth
- `middleware/rate_limiter.py` - rate limiting

❌ **Don't remove test coverage**
- Maintain or improve coverage
- Don't add untested code
- Don't disable failing tests

---

## Getting Help

### Questions About...

- **Architecture:** See [ARCHITECTURE.md](../ARCHITECTURE.md)
- **API Endpoints:** See [API_REFERENCE.md](API_REFERENCE.md)
- **Configuration:** See [CONFIGURATION.md](CONFIGURATION.md)
- **Operations:** See [OPERATIONS.md](OPERATIONS.md)
- **Infrastructure:** See [INFRASTRUCTURE.md](INFRASTRUCTURE.md)
- **Integrations:** See [INTEGRATIONS.md](INTEGRATIONS.md)

### Common Issues

- **Tests failing:** Run `pytest -v` and check error messages
- **Build failing:** Check Cloud Build logs
- **Deployment blocked:** Ensure all tests pass locally
- **OAuth issues:** Verify REDIRECT_URI matches exactly
- **Firestore errors:** Check credentials and permissions

### Contact

- **Questions:** Refer to documentation files above
- **Bugs:** Create GitHub issue with reproduction steps
- **Feature requests:** Create GitHub issue with use case
- **Production issues:** Check logs and follow emergency fix process
