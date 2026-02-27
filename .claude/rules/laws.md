# Coding Laws — NEVER VIOLATE

## Law 1: No secrets in git
- NEVER write API keys, tokens, passwords, or credentials to any file
- Use `os.getenv("SECRET_NAME")` or Secret Manager
- If you see a secret in code, replace it with env var reference immediately

## Law 2: Table ownership
- Check this repo's `database.md` for ownership table
- READ-ONLY from other repos' tables — never INSERT/UPDATE/DELETE
- Need a new column on another repo's table? Tell the user to coordinate with that repo's owner

## Law 3: All changes via PR
- NEVER commit directly to main
- NEVER use `git push origin main`
- Always create a feature branch first

## Law 4: CI must pass
- NEVER use `--no-verify` or skip pre-commit hooks
- If tests fail, fix them — don't bypass
- Run `pre-commit run --all-files` before suggesting commits

## Law 5: Rollback plan required
- For migrations, deployments, or infra changes: include rollback steps
- Ask user: "What's the rollback plan for this change?"

## Law 6: Migrations in owning repo only
- Only create Alembic migrations for tables THIS repo owns
- See Law 2 and this repo's `database.md` for ownership

## Law 7: Australian region only
- All GCP resources: `australia-southeast1`
- NEVER use `us-central1`, `us-east1`, or any non-AU region
- Check gcloud commands and Terraform for region settings

## Law 8: Change budget
- Soft limit: 15 files, 400 lines per PR
- If exceeding: warn user and suggest splitting
- Bug fix = max 5 files
- Refactor ≠ new features (don't mix)
