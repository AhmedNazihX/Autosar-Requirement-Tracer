.PHONY: dev install test lint deps deps-check

dev:
	@trap 'kill 0' EXIT INT TERM; \
	(cd backend && .venv/bin/uvicorn api.main:app --reload --port 8000) & \
	(cd frontend && npm run dev) & \
	wait

install:
	cd backend && uv sync
	cd frontend && npm install

test:
	cd backend && uv run pytest

lint:
	cd backend && uv run ruff check .
	cd frontend && npm run lint

# dependencies.txt is generated from the real resolved state of both
# projects, never hand-edited. Run this after any dependency change.
deps:
	python3 scripts/gen_dependencies.py

# Fails if dependencies.txt no longer matches the lockfiles.
deps-check:
	python3 scripts/gen_dependencies.py --check
