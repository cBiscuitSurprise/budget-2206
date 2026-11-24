-include .env
export
DAYS ?= 3
LOG := .data/serve.log
.PHONY: up down restart status logs sync

sync:
	uv run budget simplefin pull --days $(DAYS)
	uv run budget push

up:
	@mkdir -p .data
	@if pgrep -f '[.]venv/bin/budget serve' >/dev/null; then echo "serve already running"; \
	else nohup setsid uv run budget serve --allow-host fdef:c0:c0:ffd:e2f4:2ed4:53ad:7741 > $(LOG) 2>&1 < /dev/null & echo "serve: http://127.0.0.1:8765"; fi
	@echo "actual: https://actual.h.boyer.consulting (EC2 over Nebula)"

down:
	@pkill -f '[.]venv/bin/budget serve' || true

restart: down up

status:
	@if pgrep -f '[.]venv/bin/budget serve' >/dev/null; then echo "serve: up"; else echo "serve: down"; fi
	@curl -fsS https://actual.h.boyer.consulting/health && echo || echo "actual: unreachable"

logs:
	@tail -n 50 $(LOG)
