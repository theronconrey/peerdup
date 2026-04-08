.PHONY: install test clean

install:
	pip install -e ".[dev]"

test:
	pytest -v

clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -name "*.pyc" -delete
