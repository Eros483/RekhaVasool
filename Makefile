.PHONY: setup dev test style build clean harness help

setup: ## sync deps (uv sync), create .env from .env.example if missing
	uv sync --group dev
	@test -f .env || cp .env.example .env
	@echo "setup done"

dev: ## boot everything: web :8000 + voice agent :7860 + both ngrok tunnels
	@echo "== booting web (8000) + voice (7860) + ngrok =="
	@(uv run uvicorn web.app:app --reload --port 8000 > /tmp/rv_web.log 2>&1 &)
	@(uv run python voice/agent.py --transport twilio > /tmp/rv_voice.log 2>&1 &)
	@-which ngrok >/dev/null 2>&1 && (ngrok http 8000 > /tmp/rv_ngrok8000.log 2>&1 &) || echo "ngrok not found — install for webhooks"
	@-which ngrok >/dev/null 2>&1 && (ngrok http 7860 > /tmp/rv_ngrok7860.log 2>&1 &) || true
	@sleep 5
	@echo "== URLs =="
	@for port in 4040 4041; do curl -s http://127.0.0.1:$$port/api/tunnels 2>/dev/null | python3 -c "import json,sys; [print(t['public_url'],'->',t['config']['addr']) for t in json.load(sys.stdin)['tunnels']]" 2>/dev/null; done
	@echo "== web: http://localhost:8000  |  voice agent: logs in /tmp/rv_voice.log =="
	@echo "Ctrl+C stops everything (web + voice + ngrok)"
	@trap 'pkill -f "voice/agent.py"; pkill -f "uvicorn web.app"; pkill -f "ngrok http"' INT; wait

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
