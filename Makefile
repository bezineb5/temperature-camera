# Minimal Makefile for deploying TemperatureCamera to Raspberry Pi

RPI_USER ?= pi
RPI_HOST ?= rpi42.local
RPI_DIR ?= /home/$(RPI_USER)/TemperatureCamera

.DEFAULT_GOAL := help

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  %-16s %s\n", $$1, $$2}'

deploy: ## Deploy code to the Pi ($(RPI_USER)@$(RPI_HOST))
	scp -r main.py templates/ requirements.txt requirements-pi.txt TemperatureCamera.service $(RPI_USER)@$(RPI_HOST):$(RPI_DIR)/

setup-venv: ## Create venv and install dependencies on the Pi
	ssh $(RPI_USER)@$(RPI_HOST) "cd $(RPI_DIR) && python3 -m venv venv && venv/bin/pip install -r requirements.txt -r requirements-pi.txt"

service-install: ## Install and enable the systemd service on the Pi
	scp TemperatureCamera.service $(RPI_USER)@$(RPI_HOST):/tmp/
	ssh $(RPI_USER)@$(RPI_HOST) "sudo mv /tmp/TemperatureCamera.service /etc/systemd/system/ && sudo systemctl daemon-reload && sudo systemctl enable TemperatureCamera.service"

service-start: ## Start the service on the Pi
	ssh $(RPI_USER)@$(RPI_HOST) "sudo systemctl start TemperatureCamera.service"

service-stop: ## Stop the service on the Pi
	ssh $(RPI_USER)@$(RPI_HOST) "sudo systemctl stop TemperatureCamera.service"

service-restart: ## Restart the service on the Pi
	ssh $(RPI_USER)@$(RPI_HOST) "sudo systemctl restart TemperatureCamera.service"

service-status: ## Show service status on the Pi
	ssh $(RPI_USER)@$(RPI_HOST) "systemctl status TemperatureCamera.service --no-pager"

service-logs: ## Follow service logs on the Pi
	ssh $(RPI_USER)@$(RPI_HOST) "sudo journalctl -u TemperatureCamera.service -f"

service-update: ## Deploy latest code, reinstall unit if changed, restart
	$(MAKE) deploy service-install service-restart

test: ## Run unit tests locally (no hardware needed)
	python3 -m pytest tests/ -q

.PHONY: help deploy setup-venv service-install service-start service-stop service-restart service-status service-logs service-update test