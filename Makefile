.PHONY: dev install install-hooks test smoke lint deps deps-check

dev:
	@trap 'kill 0' EXIT INT TERM; \
	(cd backend && .venv/bin/uvicorn api.main:app --reload --port 8000) & \
	(cd frontend && npm run dev) & \
	wait

install: install-hooks
	cd backend && uv sync
	cd frontend && npm install

# Git does not track .git/hooks, so the hook lives in .githooks/ and this
# points git at it. Part of `install` so a fresh clone gets it without having
# to know it exists.
install-hooks:
	git config core.hooksPath .githooks
	@echo "pre-commit hook active (.githooks/pre-commit)"

test:
	cd backend && uv run pytest

# The end-to-end smoke test (story S7.1.1). Boots whatever is not already
# running, asks three real questions through the frontend proxy, runs a scoped
# report to completion, then stops what it started. Unlike `test`, this one
# reaches OpenRouter and costs a few cents.
smoke:
	python3 scripts/e2e_smoke.py

# deps-check runs here on purpose. The instruction to regenerate
# dependencies.txt was already written down in CLAUDE.md and was still missed
# for two whole work packages, so remembering is evidently not enough — this
# makes the drift fail a command that actually gets run.
lint: deps-check
	cd backend && uv run ruff check .
	cd frontend && npm run lint

# dependencies.txt is generated from the real resolved state of both
# projects, never hand-edited. Run this after any dependency change.
deps:
	python3 scripts/gen_dependencies.py

# Fails if dependencies.txt no longer matches the lockfiles.
deps-check:
	python3 scripts/gen_dependencies.py --check
