## Skill routing

When the user's request matches an available skill, invoke it via the Skill tool. When in doubt, invoke the skill.

Key routing rules:
- Product ideas/brainstorming → invoke /office-hours
- Strategy/scope → invoke /plan-ceo-review
- Architecture → invoke /plan-eng-review
- Design system/plan review → invoke /design-consultation or /plan-design-review
- Full review pipeline → invoke /autoplan
- Bugs/errors → invoke /investigate
- QA/testing site behavior → invoke /qa or /qa-only
- Code review/diff check → invoke /review
- Visual polish → invoke /design-review
- Ship/deploy/PR → invoke /ship or /land-and-deploy
- Save progress → invoke /context-save
- Resume context → invoke /context-restore
- Author a backlog-ready spec/issue → invoke /spec

## Testing

Run from the repo root: `pytest -q`.

The suite needs a Postgres it can drop and recreate (`tests/conftest.py` connects as the `postgres` superuser and rebuilds the `ivr_consent_test` database). Point `PGHOST`/`PGPORT` at it. A throwaway container works:

```bash
docker run -d --name ivr-qa-pg -e POSTGRES_HOST_AUTH_METHOD=trust -p 127.0.0.1:5435:5432 postgres:16
PGHOST=127.0.0.1 PGPORT=5435 pytest -q
```
