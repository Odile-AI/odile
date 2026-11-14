# Odile — one command that proves the tree is healthy.
#
# There is no remote CI yet, so `make check` is the gate and
# `.githooks/pre-commit` runs it on every commit. It needs /usr/bin/python3,
# shellcheck and openssl; see README.md, "Tests and the local gate".

PYTHON ?= /usr/bin/python3
SHELLCHECK ?= shellcheck
FRESH_DIR ?= $(CURDIR)/.fresh-clone
DEMO_PORT ?= 8000
DEMO_STATE ?= $(CURDIR)/demo/local/state
# The measured-image loop (docs/ops/UKI-CYCLE.md). SERVER_ID and SLUG name the
# scratch machine — <server:ukitest>, on <servury-account:dev>, never the host;
# UKI and CARD are always given on the command line. The server id is not in
# the tree: write it to /root/.odile/servury_uki_server_id (one id), or pass
# SERVER_ID=... explicitly.
SERVER_ID ?= $(shell cat /root/.odile/servury_uki_server_id 2>/dev/null)
SLUG ?= odlukitest

# Every .py and every shell script in the tree, minus VCS, virtualenvs and the
# disposable fresh-clone checkout. `.githooks/` holds shell hooks, so they are
# linted even though only one carries a .sh suffix today.
PRUNE = \( -path ./.git -o -path ./.worktrees -o -path ./.venv -o -path ./.fresh-clone \) -prune
PY_FILES = $(shell find . $(PRUNE) -o -name '*.py' -print | sort)
SH_FILES = $(shell find . $(PRUNE) -o \( -name '*.sh' -o -path './.githooks/pre-commit' \) -print | sort)

.PHONY: check acceptance prereqs test test-unit self-test compile lint claims brand roadmap secrets hooks fresh-check deploy-dry \
	demo demo-check demo-reset uki-cycle release-sign seed-cert

check: prereqs secrets test self-test compile lint claims brand roadmap
	@echo "make check: OK"

# The handoff gate. A card runs this before it asks for review, so the verifier
# reviews an artifact instead of re-driving the build. CARD=ODL-472 adds the
# per-card checks: clean tree, merges into main, evidence present.
acceptance: check
	@$(PYTHON) tools/board_sync.py check
	@if [ -n "$(CARD)" ]; then $(PYTHON) tools/card_acceptance.py --card "$(CARD)"; fi
	@echo "make acceptance: OK $(CARD)"

prereqs:
	@command -v "$(PYTHON)" >/dev/null 2>&1 || { echo "make check: $(PYTHON) is not executable (set PYTHON=...)" >&2; exit 1; }
	@command -v "$(SHELLCHECK)" >/dev/null 2>&1 || { echo "make check: $(SHELLCHECK) is not installed (apt-get install shellcheck)" >&2; exit 1; }
	@command -v openssl >/dev/null 2>&1 || { echo "make check: openssl is not installed (apt-get install openssl)" >&2; exit 1; }

# One suite: every test lives under tests/, so `discover` sees all of them.
test: test-unit

test-unit:
	$(PYTHON) -m unittest discover -s tests -v

# The installer's own suite: it verifies the shipped bytes against the
# detached signature and, in a checkout, the embedded tenant files against
# their sources. Without this step a commit could edit the installer and stay
# green with a stale pair (ODL-403). The signature check shells out to openssl,
# which prereqs names.
self-test: prereqs
	$(PYTHON) installer/odile_install.py --self-test

compile: prereqs
	@test -n "$(PY_FILES)" || { echo "make compile: no .py files found" >&2; exit 1; }
	$(PYTHON) -m compileall -q $(PY_FILES)

lint: prereqs
	@test -n "$(SH_FILES)" || { echo "make lint: no shell scripts found" >&2; exit 1; }
	$(SHELLCHECK) $(SH_FILES)

claims:
	$(PYTHON) tools/claims_lint.py

brand:
	$(PYTHON) tools/brand_lint.py

# The roadmap page is rendered from the private board, so this check runs on HQ
# and not on a public runner. Two renders at one --now against an empty board
# must be byte-identical and carry no unfilled slot; a stale template fails the
# step. In the built public tree the renderer and the backlog are absent, so the
# target prints one line and passes.
ROADMAP_NOW ?= 2026-11-13T00:00:00Z
ROADMAP_TEMPLATE ?=
roadmap:
	@if [ ! -f tools/roadmap_page.py ] || [ ! -f docs/backlog.yaml ]; then \
		echo "roadmap: skipped (tools/roadmap_page.py and docs/backlog.yaml are private)"; \
	else \
		set -eu; \
		sha=$$(git rev-parse --short HEAD); \
		dir=$$(mktemp -d); \
		trap 'rm -rf "$$dir"' EXIT; \
		$(PYTHON) -c "import sqlite3,sys; db=sqlite3.connect(sys.argv[1]); db.executescript('CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT, status TEXT, body TEXT, assignee TEXT, created_at INTEGER); CREATE TABLE task_links (parent_id TEXT, child_id TEXT);'); db.close()" "$$dir/board.db"; \
		$(PYTHON) tools/roadmap_page.py --now "$(ROADMAP_NOW)" --source-sha "$$sha" --board-db "$$dir/board.db" $(if $(ROADMAP_TEMPLATE),--template "$(ROADMAP_TEMPLATE)",) --stdout > "$$dir/one.html"; \
		$(PYTHON) tools/roadmap_page.py --now "$(ROADMAP_NOW)" --source-sha "$$sha" --board-db "$$dir/board.db" $(if $(ROADMAP_TEMPLATE),--template "$(ROADMAP_TEMPLATE)",) --stdout > "$$dir/two.html"; \
		cmp -s "$$dir/one.html" "$$dir/two.html" || { echo "roadmap: two renders at $(ROADMAP_NOW) differ" >&2; exit 1; }; \
		echo "roadmap: two renders at $(ROADMAP_NOW) are byte-identical ($$(wc -c < "$$dir/one.html") bytes)"; \
		if grep -qF '{{' "$$dir/one.html"; then echo "roadmap: the rendered page carries an unfilled slot" >&2; exit 1; fi; \
		grep -qF "generated $(ROADMAP_NOW)" "$$dir/one.html"; \
		grep -qF "source $$sha" "$$dir/one.html"; \
		echo "roadmap: every slot filled; footer names $(ROADMAP_NOW) and $$sha"; \
	fi

# The fast half of the "no secret in git" proof: every tracked file, and every
# untracked file a clone would pick up, is scanned for a private key, a provider
# token and a high-entropy value. The full-history half is one command, recorded
# in docs/evidence/m0-bench.md:
#   /usr/bin/python3 tools/secret_scan.py --history
secrets:
	$(PYTHON) tools/secret_scan.py --tree

# Enable the committed pre-commit hook in this clone. Idempotent: git keeps the
# path in local config, so run it once per clone.
hooks:
	@git rev-parse --git-dir >/dev/null 2>&1 || { echo "make hooks: run inside a git checkout" >&2; exit 1; }
	@git config core.hooksPath .githooks
	@chmod +x .githooks/pre-commit
	@echo "make hooks: pre-commit gate enabled; bypass with: git commit --no-verify"

# Clone HEAD into a throwaway checkout and run the gate there. This is the
# "works on a fresh machine" proof; commit first, because a clone sees commits,
# not the working tree.
fresh-check:
	@case "$(FRESH_DIR)" in .fresh-clone|*/.fresh-clone) ;; *) echo "make fresh-check: FRESH_DIR must be named .fresh-clone (got '$(FRESH_DIR)')" >&2; exit 1;; esac
	@echo "make fresh-check: cloning HEAD to $(FRESH_DIR)"
	@rm -rf -- "$(FRESH_DIR)"
	@git clone --quiet --no-hardlinks . "$(FRESH_DIR)"
	@$(MAKE) --no-print-directory -C "$(FRESH_DIR)" check PYTHON="$(PYTHON)" SHELLCHECK="$(SHELLCHECK)"
	@rm -rf "$(FRESH_DIR)"
	@echo "make fresh-check: a clean clone of HEAD passes make check"

deploy-dry:
	./scripts/deploy.sh -n

# The local demo: the workspace surface on one standard-library server, no
# Hermes, no NEAR key, no network. It prints the URL and a generated password,
# then serves on loopback until Ctrl-C. Files and the job log live under
# demo/local/state (override with DEMO_STATE= or ODILE_DEMO_STATE).
demo:
	$(PYTHON) demo/local/server.py --port "$(DEMO_PORT)" --state "$(DEMO_STATE)"

# The headless acceptance check: starts the real server, asserts the card's
# surface, restarts it, and waits for the 60-second job. This is what a clone
# runs; it needs no browser.
demo-check:
	$(PYTHON) demo/local/demo_check.py

# Remove the demo state. A dry run unless APPLY=1, because it deletes files.
demo-reset:
	$(PYTHON) demo/local/server.py --state "$(DEMO_STATE)" --reset $(if $(APPLY),--apply,)

# One iteration of the measured-image loop (docs/ops/UKI-CYCLE.md): publish at
# a content-addressed URL, set the boot image (the reseller API where the key
# answers, the panel otherwise), cycle the machine, capture the serial
# transcript, check the boot measurement and write the evidence block under
# docs/ops/evidence/uki-cycle/<card>/. NO_PANEL=1 stops after the publish and
# prints the pair; PANEL_DRY_RUN=1 fills the panel form without submitting;
# PANEL_DONE=1 cycles without setting the image again. SERVER_ID defaults to
# the id in /root/.odile/servury_uki_server_id (<server:ukitest>).
uki-cycle: prereqs
	@test -n "$(UKI)" || { echo "make uki-cycle: set UKI=<built .efi> CARD=<card>" >&2; exit 2; }
	@test -n "$(CARD)" || { echo "make uki-cycle: set CARD=<card>" >&2; exit 2; }
	@test -n "$(SERVER_ID)" || { echo "make uki-cycle: SERVER_ID is unset and /root/.odile/servury_uki_server_id has no id" >&2; exit 2; }
	$(PYTHON) tools/uki_cycle.py --uki "$(UKI)" --card "$(CARD)" \
		--server-id "$(SERVER_ID)" \
		$(if $(SLUG),--slug "$(SLUG)",) $(if $(VIA),--via "$(VIA)",) \
		$(if $(VCPUS),--vcpus "$(VCPUS)",) \
		$(if $(NO_PANEL),--no-panel,) $(if $(PANEL_DRY_RUN),--panel-dry-run,) \
		$(if $(PANEL_DONE),--panel-done,)

# The signing ceremony (ODL-514; docs/runbooks/seed.md §3a). Both targets
# refuse a KEY that resolves under /root: the key that signs releases and the
# seed certificate is CI-held, never on the HQ box. FILE is the release
# file to sign, or the seed public key to certify. PUBKEY overrides
# installer/odile-signing-key.pub for release-sign; seed-cert derives the public
# half from KEY unless PUBKEY names the one to verify against.
FILE ?=
KEY ?=
PUBKEY ?=
OUT ?=

release-sign:
	@$(PYTHON) seed/seed.py release-sign $(if $(FILE),--file "$(FILE)",) \
		$(if $(KEY),--key "$(KEY)",) $(if $(PUBKEY),--pubkey "$(PUBKEY)",)

seed-cert:
	@$(PYTHON) seed/seed.py seed-cert $(if $(FILE),--file "$(FILE)",) \
		$(if $(KEY),--key "$(KEY)",) $(if $(PUBKEY),--pubkey "$(PUBKEY)",) \
		$(if $(OUT),--out "$(OUT)",)
