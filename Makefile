.PHONY: install dev test winrate api build run clean

install:
	python3 -m venv .venv
	./.venv/bin/pip install --upgrade pip
	./.venv/bin/pip install -r requirements-dev.txt
	cd frontend && npm install

dev:
	./scripts/dev.sh

api:
	cd backend && ../.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload

test:
	./.venv/bin/python -m pytest backend/tests -q

winrate:
	./.venv/bin/python scripts/winrate.py --symbol BTCUSDT --days 30 --out docs/reports/winrate-btcusdt.md

build:
	./scripts/build.sh

run: build
	cd backend && ../.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000

clean:
	rm -rf .venv frontend/node_modules frontend/dist backend/static backend/data/cache
