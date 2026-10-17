# MT5 dual-terminal trading stack - one-line common tasks.
# Run `make` on its own for this help.

SHELL := /bin/bash
.DEFAULT_GOAL := help

help: ## show this help
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | \
	  awk 'BEGIN{FS=":.*?## "}{printf "  \033[1m%-12s\033[0m %s\n", $$1, $$2}'

setup: ## Linux native: packages + wine + MT5 + venv + EA (one-time)
	bash install.sh

run: ## Linux native: start panel + bridge (Ctrl+C stops all)
	bash run.sh

stop: ## stop everything (bridge, web app, terminals)
	bash run.sh stop

docker: .env data ## any OS: build image + run the container stack
	docker compose up --build

docker-up: .env data ## any OS: detached container run
	docker compose up --build -d

logs: ## follow docker compose logs
	docker compose logs -f

shell: ## shell inside the running container
	docker compose exec mt5-bridge bash

down: ## stop the container stack (state volume kept)
	docker compose down

wipe: ## stop the container stack AND delete all state
	docker compose down -v

.env:
	@printf 'MT5_MACHINE_KEY=%s\n' "$$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')" > $@
	@echo "created .env with a random MT5_MACHINE_KEY (keep it - it decrypts session.json)"

data:
	@mkdir -p data

.PHONY: help setup run stop docker docker-up logs shell down wipe
