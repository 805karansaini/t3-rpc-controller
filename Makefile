PYTHON ?= python3
VENV := .venv
VENV_PYTHON := $(VENV)/bin/python

.PHONY: help setup test check smoke live-smoke

help:
	@echo "make setup       Create .venv and install dependencies"
	@echo "make test        Run the unit tests"
	@echo "make check       Compile the source and run the tests"
	@echo "make smoke       Check the CLI and local configuration"
	@echo "make live-smoke  Read projects from every configured T3 server"

$(VENV_PYTHON): requirements.txt
	$(PYTHON) -m venv $(VENV)
	$(VENV_PYTHON) -m pip install --upgrade pip
	$(VENV_PYTHON) -m pip install -r requirements.txt

setup: $(VENV_PYTHON)

test:
	$(PYTHON) -m unittest -v

check:
	$(PYTHON) -m py_compile t3_rpc_controller.py test_t3_rpc_controller.py test_t3_operations.py test_t3_transport.py
	$(MAKE) test

smoke:
	./t3_rpc_controller.py --help >/dev/null
	./t3_rpc_controller.py environments

live-smoke:
	./t3_rpc_controller.py health >/dev/null
	./t3_rpc_controller.py compatibility >/dev/null
	./t3_rpc_controller.py projects >/dev/null
	./t3_rpc_controller.py threads --environment mac --limit 1 >/dev/null
	./t3_rpc_controller.py threads --environment i7 --limit 1 >/dev/null
	./t3_rpc_controller.py providers >/dev/null
	@echo "Mac and i7 read-only smoke checks passed"
