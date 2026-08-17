# Trading Agent — Makefile shortcuts (Mac/Linux)
# Not required — just convenient aliases.
#
# Usage:
#   make setup    — First-time setup (venv + deps + .env)
#   make start    — Start the agent locally
#   make docker   — Start with Docker (background)
#   make logs     — Stream Docker logs
#   make stop     — Stop Docker container
#   make check    — Validate .env keys

.PHONY: setup start docker logs stop check clean

setup:
	python3 setup.py

start:
	python3 start.py

check:
	python3 start.py --check

docker:
	docker compose up -d --build
	@echo ""
	@echo "Agent running in background. Stream logs with:  make logs"

logs:
	docker compose logs -f

stop:
	docker compose down

clean:
	rm -rf .venv __pycache__ **/__pycache__ logs/*.log
