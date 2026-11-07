.PHONY: check test lint scan demo demo-agent hooks clean-demo

PY ?= python3
DEMO_DIR ?= ./demo-data

# The one command an agent (or you) must pass before pushing: lint, the
# offline test suite (includes the eval checkers, which need no model key),
# and the identifier scan with .identifiers.local if present.
check: lint test scan

test:
	$(PY) -m pytest -q -p no:cacheprovider

lint:
	ruff check --no-cache .

scan:
	$(PY) scripts/scan_identifiers.py

# Synthetic store plus the home-3day example. No model key, no network,
# no personal data. Writes only to $(DEMO_DIR) (git-ignored).
demo:
	$(PY) scripts/make_synthetic_data.py --out $(DEMO_DIR) --example home-3day --end today
	COACH_DATA_DIR=$(DEMO_DIR) bin/coach check
	@echo; echo "--- what the coach receives at the start of a conversation (first 40 lines) ---"
	@COACH_DATA_DIR=$(DEMO_DIR) bin/coach context | head -40
	@echo; echo "--- a tool call, no model involved ---"
	@cd agent && COACH_DATA_DIR=../$(DEMO_DIR) $(PY) -c "import mcp_server as m; print(m.get_exercise_trend('Squat')[:600])"
	@echo; echo "Next: make demo-agent (needs a model key), or bin/coach init for your own instance."

# The full agent over the demo store. Needs agent/requirements.txt installed
# and a key for COACH_MODEL in the environment (see .env.example).
demo-agent:
	cd agent && COACH_DATA_DIR=../$(DEMO_DIR) uvicorn service:app --port 8000

# Run the scan before every push from this checkout.
hooks:
	ln -sf ../../scripts/pre-push .git/hooks/pre-push
	@echo "pre-push hook installed"

clean-demo:
	rm -rf $(DEMO_DIR)
