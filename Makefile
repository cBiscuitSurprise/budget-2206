COMPOSE := docker compose -f actual.docker-compose.yml
LOG := .data/serve.log
.PHONY: up down restart status logs

up:
	@mkdir -p .data
	@if pgrep -f '[.]venv/bin/budget serve' >/dev/null; then echo "serve already running"; \
	else nohup setsid uv run budget serve --allow-host fdef:c0:c0:ffd:e2f4:2ed4:53ad:7741 > $(LOG) 2>&1 < /dev/null & echo "serve: http://127.0.0.1:8765"; fi
	@$(COMPOSE) up -d
	@echo "actual: http://127.0.0.1:5006"

down:
	@pkill -f '[.]venv/bin/budget serve' || true
	@$(COMPOSE) down

restart: down up

status:
	@if pgrep -f '[.]venv/bin/budget serve' >/dev/null; then echo "serve: up"; else echo "serve: down"; fi
	@$(COMPOSE) ps

logs:
	@tail -n 50 $(LOG)
