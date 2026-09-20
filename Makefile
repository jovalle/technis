.DEFAULT_GOAL := prepare
.PHONY: prepare build check

prepare:
	bash scripts/tctl/prepare

build:
	bash scripts/tctl/prepare --source

check:
	cargo fmt --all --check
	cargo clippy --locked --all-targets -- -D warnings
	cargo test --locked
	python3 scripts/tctl/test_prepare.py
	python3 scripts/tctl/test_terminal.py target/debug/tctl
