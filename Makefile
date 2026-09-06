.PHONY: up down logs build migrate revision seed bootstrap test lint fmt shell

up:            ## start the full local stack
	docker compose up -d --build
down:
	docker compose down
logs:
	docker compose logs -f api worker-extract worker-profile beat
build:
	docker compose build

migrate:
	docker compose run --rm api alembic upgrade head
revision:
	docker compose run --rm api alembic revision --autogenerate -m "$(m)"
seed:
	docker compose run --rm api python scripts/seed_taxonomy.py
bootstrap:
	docker compose run --rm api python scripts/bootstrap.py

init: up migrate seed bootstrap   ## first-run setup

test:
	pytest -q
lint:
	ruff check src tests && mypy src
fmt:
	ruff format src tests && ruff check --fix src tests
shell:
	docker compose run --rm api python
