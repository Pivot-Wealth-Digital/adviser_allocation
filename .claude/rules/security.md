---
globs:
  - "src/**"
  - "tests/**"
---
# Security Rules

## Secrets
- Use `os.getenv()` for all credentials
- NEVER hardcode: API keys, tokens, passwords, connection strings
- Check for secrets before every commit

## SQL Injection
- ALWAYS use parameterised queries: `cursor.execute("SELECT * FROM t WHERE id = %s", (id,))`
- NEVER use f-strings or .format() with SQL: `f"SELECT * FROM t WHERE id = {id}"` ← FORBIDDEN

## Input Validation
- Validate all user input at API boundaries
- Use Pydantic or explicit validation for request data
- Sanitise before database operations

## Error Handling
- Log full errors server-side: `logger.exception("Error in handler")`
- Return generic errors to clients: `{"error": "Internal server error"}`
- NEVER expose stack traces, SQL errors, or internal paths to clients

## Logging
- Use `logger` module, NEVER `print()`
- NEVER log: passwords, tokens, PII, full credit card numbers
- OK to log: user IDs, request IDs, operation names, timing
