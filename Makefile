# Makefile for Local Development

SHELL := /bin/bash
PYTHON := .venv/bin/python
PIP := .venv/bin/pip

.PHONY: help venv install run smoke logs smoke-logs clean

help:
	@echo "Available commands:"
	@echo "  make venv        Create virtual environment"
	@echo "  make install     Install dependencies"
	@echo "  make run         Run bot in local mode (dry-run)"
	@echo "  make smoke       Run smoke test (30s)"
	@echo "  make logs        Tail run logs"
	@echo "  make smoke-logs  Tail smoke logs"
	@echo "  make clean       Remove .venv and caches"

venv:
	python3 -m venv .venv
	@echo "Virtual environment created in .venv"

install: venv
	$(PIP) install --upgrade pip
	$(PIP) install -r requirements.txt
	@echo "Dependencies installed"

run:
	@mkdir -p logs
	@echo "Starting local run (Dry Run)..."
	ENV=local ENVIRONMENT=dev SYSTEM_DRY_RUN=true LOG_LEVEL=INFO $(PYTHON) run.py live --force --log-file logs/run.log | tee -a logs/run.log

smoke:
	@mkdir -p logs
	@echo "Starting smoke test (30s)..."
	ENV=local ENVIRONMENT=dev SYSTEM_DRY_RUN=true RUN_SECONDS=30 LOG_LEVEL=DEBUG $(PYTHON) run.py live --force --log-file logs/smoke.log | tee -a logs/smoke.log

logs:
	tail -n 200 -f logs/run.log

smoke-logs:
	tail -n 200 -f logs/smoke.log

clean:
	rm -rf .venv
	rm -rf __pycache__
	rm -rf .local
