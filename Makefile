.PHONY: check default fmt fmt-all fmt-js fmt-json fmt-make fmt-py fmt-toml help init install-tflint lint lint-json lint-md lint-terraform lint-toml pre-commit setup test venv web-build web-commit web-deps web-dev

DEPS := \
	age \
	ansible \
	direnv \
	flux \
	fzf \
	git \
	helmfile \
	jq \
	kubectl \
	node \
	npm \
	pip3 \
	pre-commit \
	python3 \
	ruff \
	sops \
	talosctl \
	task \
	terraform \
	tflint

# Check all dependencies
check:
	@echo "Checking required dependencies..."
	@echo ""
	@missing=0; \
	for dep in $(DEPS); do \
		if command -v $$dep >/dev/null 2>&1; then \
			echo "  ✓ $$dep"; \
		else \
			echo "  ✗ $$dep (not found)"; \
			missing=$$((missing + 1)); \
		fi; \
	done; \
	echo ""; \
	if [ -d .venv ]; then \
		echo "  ✓ .venv (Python virtual environment)"; \
		if .venv/bin/python3 -c "import ruamel.yaml" >/dev/null 2>&1; then \
			echo "  ✓ ruamel.yaml (available in virtual environment)"; \
		else \
			echo "  ✗ ruamel.yaml (not available in virtual environment)"; \
			missing=$$((missing + 1)); \
		fi; \
	else \
		echo "  ✗ .venv (run: make setup)"; \
		missing=$$((missing + 1)); \
	fi; \
	if [ -f .git/hooks/pre-commit ]; then \
		echo "  ✓ pre-commit hooks installed"; \
	else \
		echo "  ✗ pre-commit hooks (run: make setup)"; \
		missing=$$((missing + 1)); \
	fi; \
	echo ""; \
	echo "Optional dependencies (enhance UX):"; \
	echo ""; \
	for dep in $(OPTIONAL_DEPS); do \
		if command -v $$dep >/dev/null 2>&1; then \
			echo "  ✓ $$dep"; \
		else \
			echo "  ○ $$dep (not installed - brew install $$dep)"; \
		fi; \
	done; \
	echo ""; \
	if [ $$missing -eq 0 ]; then \
		echo "All required dependencies satisfied ✓"; \
	else \
		echo "Missing $$missing required dependencies"; \
		exit 1; \
	fi

## Default target
default: check

# Format files to personal code style
fmt: fmt-all fmt-js fmt-json fmt-make fmt-py fmt-toml

fmt-all:
	@echo "Formatting JavaScript, Markdown, HTML, CSS, YAML files with Prettier..."
	npm run fmt

## Format JSON files specifically
fmt-json:
	@echo "Formatting JSON files with Prettier..."
	npm run fmt:json

## Format Makefile
fmt-make:
	@echo "Formatting Makefile..."
	@.venv/bin/python3 scripts/makefmt.py

## Format Python files
fmt-py:
	@echo "Formatting Python files..."
	@ruff format $(shell git ls-files "*.py" | grep -v -E "^(archive|docs|web)/" | while read f; do [ -f "$$f" ] && echo "$$f"; done)

## Format TOML files
fmt-toml:
	@echo "Formatting TOML files with Taplo..."
	npm run fmt:toml

## Show available commands
help:
	@echo "Technis - Homelab Infrastructure"
	@echo ""
	@echo "Commands:"
	@awk '/^# [^#]/ { desc = substr($$0, 3); getline; if ($$0 ~ /^[a-zA-Z_-]+:/) { target = $$1; sub(/:.*/, "", target); printf "  make %-12s - %s\n", target, desc } }' Makefile
	@echo ""
	@echo "After dependencies are installed:"
	@echo "  task             - List all available tasks"
	@echo "  task <name>      - Run a specific task"
	@echo ""

# Initialize git submodules
init:
	@git submodule update --init --recursive
	@git submodule status

## Install tflint
install-tflint:
	@echo "Installing tflint..."
	@if command -v brew >/dev/null 2>&1; then \
		brew install tflint; \
	else \
		@echo "Homebrew not found. Please install Homebrew first or install tflint manually."; \
		exit 1; \
	fi

## Lint files
lint: lint-md lint-json lint-toml lint-terraform

## Lint JSON files
lint-json:
	@echo "Linting JSON files..."
	npm run lint:json

## Lint Markdown files
lint-md:
	@echo "Linting Markdown files..."
	@markdownlint --fix --config .markdownlint.yaml README.md docs/src

## Lint Terraform files
lint-terraform:
	@echo "Linting Terraform files..."
	@if command -v tflint >/dev/null 2>&1; then \
		tflint --init; \
		tflint; \
	else \
		echo "tflint not found. Run 'make install-tflint' to install it."; \
		exit 1; \
	fi

## Lint TOML files
lint-toml:
	@echo "Linting TOML files..."
	npm run lint:toml

# Run pre-commit hooks on all files
pre-commit:
	@echo "Running pre-commit hooks on all files..."
	@pre-commit run --all-files

# Install all dependencies
setup: venv
	@echo "Setting up dependencies..."
	.venv/bin/pip install -r requirements.txt
	# Verify ruamel.yaml is properly installed in the virtual environment
	.venv/bin/python3 -c "import ruamel.yaml; print('ruamel.yaml version:', ruamel.yaml.__version__)"
	npm install
	cd web && npm install
	pre-commit install
	make install-tflint
	@echo "Dependencies installed successfully ✓"

## Run tests using the dynamic test runner
test:
	@echo "Running tests with dynamic test runner..."
	@.venv/bin/python scripts/tests/run_tests.py

## Create Python virtual environment if it doesn't exist
venv:
	@if [ ! -d .venv ]; then \
		echo "Creating Python virtual environment..."; \
		python3 -m venv .venv; \
		.venv/bin/pip install --upgrade pip; \
		echo "Virtual environment created ✓"; \
	else \
		echo "Virtual environment already exists ✓"; \
	fi

## Build web assets
web-build:
	@echo "Building web assets..."
	cd web && make build

## Commit using web directory
web-commit:
	@echo "Creating commit in web directory..."
	cd web && make commit

## Install web dependencies
web-deps:
	@echo "Installing web dependencies..."
	cd web && make install

## Run web development server
web-dev:
	@echo "Starting web development server..."
	cd web && make dev
