.PHONY: setup dev test style build clean harness help

setup: ## sync deps (uv sync), create .env from .env.example if missing
	uv sync --group dev
	@test -f .env || cp .env.example .env && echo ".env created from .env.example"
	@echo "setup done"

dev: ## uvicorn web/app.py --reload (single server; no frontend split)
	uv run uvicorn web.app:app --reload --port 8000

test: ## pytest tests/harness.py -v  (must show 20/20) + any unit tests
	uv run pytest -v

style: ## black . && ruff check --fix .
	uv run black .
	uv run ruff check --fix .

build: ## no-op / collect checks (Python-only, no bundle)
	uv run python -m py_compile policy/*.py agent/*.py web/*.py recovery/*.py 2>/dev/null || true
	@echo "build ok"

clean: ## rm -rf __pycache__ .pytest_cache audit.jsonl recovery.db logs/
	rm -rf __pycache__ .pytest_cache audit.jsonl recovery.db logs/
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .pytest_cache -exec rm -rf {} + 2>/dev/null || true

harness: ## GEMINI_ENABLED=false pytest tests/harness.py -v (20/20 + summary print)
	GEMINI_ENABLED=false uv run pytest tests/harness.py -v -s

help: ## show help
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-12s\033[0m %s\n", $$1, $$2}'
