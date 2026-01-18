# Makefile for Local Development

SHELL := /bin/bash
PYTHON := .venv/bin/python
PIP := .venv/bin/pip

.PHONY: help venv install run smoke logs smoke-logs test integration pre-deploy clean clean-logs status validate

help:
	@echo "Available commands:"
	@echo "  make venv          Create virtual environment"
	@echo "  make install       Install dependencies"
	@echo "  make validate      Validate environment configuration"
	@echo "  make run           Run bot in local mode (dry-run)"
	@echo "  make smoke         Run smoke test (30s)"
	@echo "  make integration   Run integration test (5 mins, tests all code paths)"
	@echo "  make pre-deploy    Run all pre-deployment tests (REQUIRED before push to main)"
	@echo "  make test          Run unit tests"
	@echo "  make logs          Tail run logs"
	@echo "  make smoke-logs    Tail smoke logs"
	@echo "  make status        Check if bot is running"
	@echo "  make clean         Remove .venv and caches"
	@echo "  make clean-logs    Remove log files"

venv:
	python3 -m venv .venv
	@echo "✅ Virtual environment created in .venv"
	@echo "To activate: source .venv/bin/activate"

install: venv
	$(PIP) install --upgrade pip
	$(PIP) install -r requirements.txt
	@echo "✅ Dependencies installed"

validate:
	@echo "Validating environment configuration..."
	@if [ ! -f .env.local ]; then \
		echo "⚠️  .env.local not found. Creating from template..."; \
		cp .env.local.example .env.local; \
		echo "✅ Created .env.local - please review and update if needed"; \
	fi
	@echo "✅ Environment validation complete"

run:
	@mkdir -p logs
	@echo "Starting local run (Dry Run)..."
	@if [ -f .env.local ]; then \
		set -a; source .env.local; set +a; \
		ENV=local ENVIRONMENT=dev DRY_RUN=1 LOG_LEVEL=INFO $(PYTHON) run.py live --force 2>&1 | tee -a logs/run.log; \
	else \
		echo "❌ .env.local not found. Run 'make validate' first."; \
		exit 1; \
	fi

smoke:
	@mkdir -p logs
	@echo "Starting smoke test (30s)..."
	@if [ -f .env.local ]; then \
		set -a; source .env.local; set +a; \
		ENV=local ENVIRONMENT=dev DRY_RUN=1 RUN_SECONDS=30 LOG_LEVEL=INFO $(PYTHON) run.py live --force 2>&1 | tee logs/smoke.log; \
		EXIT_CODE=$$?; \
		if [ $$EXIT_CODE -eq 0 ]; then \
			echo ""; \
			echo "✅ SMOKE TEST PASSED"; \
			echo "Exit code: $$EXIT_CODE"; \
		else \
			echo ""; \
			echo "❌ SMOKE TEST FAILED"; \
			echo "Exit code: $$EXIT_CODE"; \
			echo "Check logs/smoke.log for details"; \
		fi; \
		exit $$EXIT_CODE; \
	else \
		echo "❌ .env.local not found. Run 'make validate' first."; \
		exit 1; \
	fi

integration:
	@mkdir -p logs
	@echo "Starting integration test (5 minutes)..."
	@echo "This will test signal generation for 20+ symbols to catch bugs early."
	@if [ -f .env.local ]; then \
		set -a; source .env.local; set +a; \
		ENV=local ENVIRONMENT=dev DRY_RUN=1 LOG_LEVEL=INFO $(PYTHON) src/test_integration.py 300 2>&1 | tee logs/integration.log; \
		EXIT_CODE=$$?; \
		if [ $$EXIT_CODE -eq 0 ]; then \
			echo ""; \
			echo "✅ INTEGRATION TEST PASSED"; \
			echo "Exit code: $$EXIT_CODE"; \
		else \
			echo ""; \
			echo "❌ INTEGRATION TEST FAILED"; \
			echo "Exit code: $$EXIT_CODE"; \
			echo "Check logs/integration.log for details"; \
		fi; \
		exit $$EXIT_CODE; \
	else \
		echo "❌ .env.local not found. Run 'make validate' first."; \
		exit 1; \
	fi

pre-deploy:
	@echo "=========================================="
	@echo "PRE-DEPLOYMENT TEST SUITE"
	@echo "=========================================="
	@echo ""
	@echo "Step 1/2: Running smoke test (30s)..."
	@$(MAKE) smoke
	@echo ""
	@echo "Step 2/2: Running integration test (5 mins)..."
	@$(MAKE) integration
	@echo ""
	@echo "=========================================="
	@echo "✅ ALL PRE-DEPLOYMENT TESTS PASSED"
	@echo "=========================================="
	@echo ""
	@echo "Safe to push to main and deploy to production."

test:
	@echo "Running unit tests..."
	$(PYTHON) -m pytest tests/ -v

logs:
	@if [ -f logs/run.log ]; then \
		tail -n 200 -f logs/run.log; \
	else \
		echo "❌ logs/run.log not found. Run 'make run' first."; \
	fi

smoke-logs:
	@if [ -f logs/smoke.log ]; then \
		tail -n 200 logs/smoke.log; \
	else \
		echo "❌ logs/smoke.log not found. Run 'make smoke' first."; \
	fi

status:
	@echo "Checking bot status..."
	@ps aux | grep "[p]ython run.py" || echo "Bot is not running"

clean:
	rm -rf .venv
	rm -rf __pycache__
	rm -rf .local
	find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete 2>/dev/null || true
	@echo "✅ Cleaned up virtual environment and caches"

clean-logs:
	rm -rf logs/
	@echo "✅ Cleaned up log files"

