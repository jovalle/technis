.PHONY: prepare build clean dev serve copy watch

prepare:
	@if ! command -v npm >/dev/null 2>&1; then \
		echo "npm is not installed. Please install Node.js and npm before proceeding."; \
		exit 1; \
	fi

build: prepare
	npm run build

clean: prepare
	npm run clean

dev: prepare
	npm run dev

serve: prepare
	npm run serve

copy: prepare
	npm run copy

watch: prepare
	npm run watch
