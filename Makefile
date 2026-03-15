SHELL := /bin/bash
PY := .venv/bin/python
PIP := .venv/bin/pip

.DEFAULT_GOAL := help

help:
	@echo ""
	@echo "Session Sync: Google Calendar → Airtable"
	@echo ""
	@echo "Targets:"
	@echo "  make bootstrap     Create venv + install deps"
	@echo "  make doctor        Check env vars + required files"
	@echo "  make dryrun        Dry run, last 4 weeks (safe, no writes)"
	@echo "  make report        Dry run + no-match report"
	@echo "  make apply         Write to Airtable (requires APPLY=1)"
	@echo "  make dryrun1       Dry run, last 1 week"
	@echo "  make dryrun12      Dry run, last 12 weeks"
	@echo "  make freeze        Write requirements.txt"
	@echo ""

bootstrap:
	@./scripts/bootstrap.sh

install: .venv
	@$(PIP) install --upgrade pip
	@$(PIP) install -r requirements.txt

.venv:
	@python3 -m venv .venv

doctor:
	@$(PY) scripts/doctor.py

dryrun:
	@$(PY) session_sync.py --dry-run --weeks 4 --calendar-id primary

dryrun1:
	@$(PY) session_sync.py --dry-run --weeks 1 --calendar-id primary

dryrun4:
	@$(PY) session_sync.py --dry-run --weeks 4 --calendar-id primary

dryrun12:
	@$(PY) session_sync.py --dry-run --weeks 12 --calendar-id primary

report:
	@$(PY) session_sync.py --dry-run --weeks 4 --calendar-id primary --report-no-match

apply:
	@[ "$$APPLY" = "1" ] || (echo "Refusing to apply. Run: APPLY=1 make apply"; exit 1)
	@$(PY) session_sync.py --apply --weeks 4 --calendar-id primary

freeze:
	@$(PIP) freeze > requirements.txt
	@echo "Wrote requirements.txt"
