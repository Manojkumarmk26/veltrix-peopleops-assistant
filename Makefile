.PHONY: help install test test-cov run dev docker-up docker-down docker-build clean

# Override to use a specific interpreter, e.g. make test PYTHON=./conda_env/python.exe
PYTHON ?= python

help:
	@echo "install      Install Python dependencies"
	@echo "test         Run the test suite (pytest)"
	@echo "test-cov     Run tests with coverage"
	@echo "run          Start the API on port 8000"
	@echo "docker-up    Build and start with docker compose"

install:
	$(PYTHON) -m pip install -r requirements.txt

# Run tests with pytest (required by assessment)
test:
	$(PYTHON) -m pytest tests/ -v --tb=short

# Run tests with coverage
test-cov:
	$(PYTHON) -m pytest tests/ -v --tb=short --cov=app --cov-report=term-missing

# Run development server locally
dev:
	$(PYTHON) -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload

# Run production server locally (single worker: sessions are in memory)
run:
	$(PYTHON) -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1

# Docker commands
docker-up:
	docker compose up --build

docker-down:
	docker compose down

docker-build:
	docker compose build

# Clean up generated files (keeps ./data/veltrix.db and the corpus)
clean:
	find . -type d -name __pycache__ -not -path "./conda_env/*" -exec rm -rf {} + 2>/dev/null || true
	rm -rf .pytest_cache htmlcov .coverage .cache data/vector_store
