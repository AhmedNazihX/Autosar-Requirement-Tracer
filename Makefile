.PHONY: dev install test lint

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
