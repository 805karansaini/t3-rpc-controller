PYTHON ?= python3
VENV := .venv
VENV_PYTHON := $(VENV)/bin/python
VENV_READY := $(VENV)/.ready

.PHONY: help setup test check smoke live-smoke

help:
	@echo "make setup       Create .venv and install dependencies"
	@echo "make test        Run the unit tests"
	@echo "make check       Compile the source and run the tests"
	@echo "make smoke       Check the CLI and local configuration"
	@echo "make live-smoke  Read projects from every configured T3 server"

$(VENV_READY): requirements.txt pyproject.toml
	$(PYTHON) -m venv $(VENV)
	$(VENV_PYTHON) -m pip install --upgrade pip
	$(VENV_PYTHON) -m pip install -r requirements.txt
	@touch $(VENV_READY)

setup: $(VENV_READY)

test: setup
	$(VENV_PYTHON) -m unittest -v

check: setup
	$(VENV_PYTHON) -m compileall -q t3_controller t3_rpc_controller.py test_t3_rpc_controller.py test_t3_operations.py test_t3_transport.py
	$(VENV_PYTHON) -m pip check
	$(MAKE) test

smoke: setup
	$(VENV)/bin/t3ctl --help >/dev/null
	$(VENV)/bin/t3ctl environments

live-smoke: setup
	$(VENV)/bin/t3ctl health >/dev/null
	$(VENV)/bin/t3ctl compatibility >/dev/null
	$(VENV)/bin/t3ctl projects >/dev/null
	$(VENV)/bin/t3ctl threads --environment mac --limit 1 >/dev/null
	$(VENV)/bin/t3ctl threads --environment i7 --limit 1 >/dev/null
	$(VENV)/bin/t3ctl providers >/dev/null
	@echo "Mac and i7 read-only smoke checks passed"
