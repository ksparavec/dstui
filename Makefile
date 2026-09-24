SHELL := /bin/bash

# Single source of truth for the exact Python version, X.Y.Z: the dev venv, CI, the locks and
# the bundled installer all derive from .python-version (edit it there; the build fails unless
# the bundled interpreter is exactly this version). A patch bump also needs a uv that knows the
# new CPython: locally, and the setup-uv `version` in .github/workflows/release.yml. Then re-run
# `make dev-install` (the test suite fails on any other interpreter).
PYTHON_VERSION := $(shell cat .python-version)

# Prefer .venv/bin/* when present (dev-install), else fall back to PATH.
PYTHON := $(if $(wildcard .venv/bin/python),.venv/bin/python,python3)
PYTEST := $(if $(wildcard .venv/bin/pytest),.venv/bin/pytest,pytest)
RUFF   := $(if $(wildcard .venv/bin/ruff),.venv/bin/ruff,ruff)
MYPY   := $(if $(wildcard .venv/bin/mypy),.venv/bin/mypy,mypy)
BANDIT := $(if $(wildcard .venv/bin/bandit),.venv/bin/bandit,bandit)

# Extra pytest arguments: make test PYTEST_ARGS='-m "not e2e"'. The suite itself keeps every
# temp file under /var/tmp and removes it afterwards (tests/tmp_hygiene.py, see CLAUDE.md).
PYTEST_ARGS ?=

.PHONY: help dev-install lock test test-cov lint lint-fix typecheck security check package release clean

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  %-18s %s\n", $$1, $$2}'

# --- Setup ---

# A fresh venv on exactly .python-version (--clear: a venv left on another patch must not
# survive; uv's errors stay visible), then exactly the hash-checked versions of
# requirements-dev.txt (the tests run on what ships), then dstui itself, editable, built by the
# hash-pinned backend of requirements-build.txt.
dev-install: ## Set up .venv with dstui + dev dependencies from the locks (editable; incl. the test-only runtime)
	uv venv --clear --python $(PYTHON_VERSION) .venv
	uv pip sync --python .venv/bin/python --require-hashes requirements-dev.txt
	uv pip install --python .venv/bin/python --no-deps --build-constraints requirements-build.txt -e .

# --python-version pins the resolution to .python-version instead of whatever
# interpreter happens to be active.
# requirements.txt is exactly what the installer bundles, so it must never carry the SDK's
# embedded runtime (the SDK is installed --no-deps; dsh is a separate install).
# requirements-dev.txt keeps it: `make dev-install` and CI install it for the e2e tests.
# requirements-build.txt pins the build backend (pyproject [build-system]) that builds the
# shipped wheel.
lock: ## Regenerate requirements.txt, requirements-dev.txt and requirements-build.txt from pyproject.toml
	uv pip compile pyproject.toml --python-version $(PYTHON_VERSION) --generate-hashes \
		--no-emit-package deepseek-harness-runtime-bin -o requirements.txt
	uv pip compile pyproject.toml --python-version $(PYTHON_VERSION) --extra dev --generate-hashes -o requirements-dev.txt
	$(PYTHON) -c 'import tomllib; print(*tomllib.load(open("pyproject.toml", "rb"))["build-system"]["requires"], sep="\n")' | \
		uv pip compile - --python-version $(PYTHON_VERSION) --generate-hashes -o requirements-build.txt

# --- Testing ---

test: ## Run tests (hermetic; temp files under /var/tmp, removed afterwards)
	$(PYTEST) tests/ $(PYTEST_ARGS)

test-cov: ## Run tests with coverage (term-missing; fails below 90 %)
	$(PYTEST) tests/ --cov=dstui --cov-report=term-missing $(PYTEST_ARGS)

# --- Static checks ---

lint: ## Run ruff linter + format check
	$(RUFF) check src/ tests/
	$(RUFF) format --check src/ tests/

lint-fix: ## Run ruff with auto-fix + format
	$(RUFF) check --fix src/ tests/
	$(RUFF) format src/ tests/

typecheck: ## Run mypy (strict)
	$(MYPY) src/

security: ## Run bandit
	$(BANDIT) -r src/ -c pyproject.toml

check: lint typecheck security ## Static checks: ruff + mypy + bandit (what the CI lint job runs)

# --- Packaging ---

package: ## Build the self-contained, precompiled installer -> dist/dstui-install.sh
	@bash tools/package/build-binary.sh

# --- Release ---

release: ## Cut a release: promote CHANGELOG, tag + push; the tag makes CI build, attest and publish it (version from pyproject.toml)
	@bash tools/release/release.sh

# --- Cleanup ---

clean: ## Remove generated and cached files
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	rm -rf .pytest_cache .mypy_cache .ruff_cache
	rm -rf htmlcov .coverage .coverage.*
	rm -rf build dist *.egg-info src/*.egg-info
	rm -rf .venv
	@echo "Clean complete"
