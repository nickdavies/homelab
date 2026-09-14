.PHONY: help build install test lint format

help:
	@echo "Targets:"
	@echo "  build   - cargo build --release"
	@echo "  install - cargo install --path . (puts talos-session on PATH)"
	@echo "  test    - cargo test"
	@echo "  lint    - cargo clippy --all-targets -- -D warnings"
	@echo "  format  - cargo fmt"

build:
	cargo build --release

install:
	cargo install --path . --locked

test:
	cargo test

lint:
	cargo clippy --all-targets -- -D warnings

format:
	cargo fmt
