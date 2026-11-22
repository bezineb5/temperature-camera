# Minimal Makefile for deploying TemperatureCamera to Raspberry Pi

RPI_USER ?= pi
RPI_HOST ?= rpi42.local
RPI_DIR ?= /home/$(RPI_USER)/TemperatureCamera

deploy:
	scp -r main.py templates/ requirements.txt requirements-pi.txt TemperatureCamera.service $(RPI_USER)@$(RPI_HOST):$(RPI_DIR)/

setup-venv:
	ssh $(RPI_USER)@$(RPI_HOST) "cd $(RPI_DIR) && python3 -m venv venv && venv/bin/pip install -r requirements.txt -r requirements-pi.txt"

test:
	python3 -m pytest tests/ -q

.PHONY: deploy setup-venv test