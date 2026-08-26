.PHONY: help lint test synth security deploy-dev deploy-prod clean all

ENV ?= dev

help:
	@echo "make all         lint, test, synth  (run this before pushing)"
	@echo "make test        unit + infrastructure tests with coverage"
	@echo "make deploy-dev  deploy to the dev account"
	@echo "make deploy-prod deploy to the prod account"

lint:
	ruff check .
	ruff format --check .

test:
	pytest -q --cov=src --cov=infra --cov-report=term-missing --cov-fail-under=80

synth:
	cdk synth -c env=$(ENV) >/dev/null

security: synth
	checkov -d cdk.out --quiet --compact --framework cloudformation --config-file .checkov.yaml

deploy-dev:
	cdk deploy -c env=dev --require-approval never

deploy-prod:
	cdk deploy -c env=prod --require-approval never

clean:
	rm -rf cdk.out .pytest_cache .ruff_cache .coverage htmlcov

all: lint test synth
