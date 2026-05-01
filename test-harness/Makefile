.PHONY: up down test reset-mocks clean install logs

install:
	pip install -r requirements.txt

up:
	docker compose up -d
	@echo "n8n:     http://localhost:5678"
	@echo "wa-mock: http://localhost:9999/health"
	@sleep 3
	@curl -s http://localhost:9999/health || echo "(wa-mock still starting...)"

down:
	docker compose down

logs:
	docker compose logs -f --tail=50

test:
	@if [ -z "$(SPEC)" ]; then echo "usage: make test SPEC=specs/reorder-reminder.example.yml"; exit 1; fi
	python runner.py $(SPEC)

test-all:
	@for spec in specs/*.yml; do \
		echo "=== $$spec ==="; \
		python runner.py $$spec || exit 1; \
	done

reset-mocks:
	curl -s -X POST http://localhost:9999/reset

clean:
	docker compose down -v
	@echo "n8n_test_data volume removed"
