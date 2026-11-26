# Odile — one command that proves the tree is healthy.
#
# `make check` is the full gate; `.githooks/pre-commit` runs the fast half
# (`make check-fast`) on every commit, and `.github/workflows/check.yml` runs
# the full one on every push and on every pull request to `main`. The full gate
# also runs in `card_acceptance.py --full` — on the merge tree, inside
# `make acceptance`, so the stamp names the tree that lands — and at the merge
# gate. It needs /usr/bin/python3, shellcheck and openssl; see CONTRIBUTING.md.

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

# The gate's file lists come from the index, not from a walk of the working
# tree: `git ls-files` is the repository, so an untracked private evidence
# script (the shared checkout keeps the operating record under `docs/ops/`
# untracked) cannot fail the gate every merge runs (ODL-338). The hooks carry no
# `.sh` suffix, so each is named explicitly; every script that does carry the
# suffix is already in the list. A tree with no git — the built public tree —
# keeps the walk, pruned of VCS, virtualenvs and the disposable fresh-clone
# checkout, so it still lints every script and compiles every module it ships.
GIT ?= git
PRUNE = \( -path ./.git -o -path ./.worktrees -o -path ./.venv -o -path ./.fresh-clone \) -prune
ifeq ($(shell $(GIT) rev-parse --is-inside-work-tree 2>/dev/null),true)
PY_FILES = $(shell $(GIT) ls-files -- '*.py')
SH_FILES = $(shell { $(GIT) ls-files -- '*.sh'; $(GIT) ls-files -- .githooks/pre-commit .githooks/commit-msg .githooks/pre-push; } | sort -u)
else
PY_FILES = $(shell find . $(PRUNE) -o -name '*.py' -print | sort)
SH_FILES = $(shell find . $(PRUNE) -o \( -name '*.sh' -o -path './.githooks/pre-commit' -o -path './.githooks/commit-msg' -o -path './.githooks/pre-push' \) -print | sort)
endif

.PHONY: check check-fast check-hq check-inner acceptance prereqs test test-fast test-unit self-test compile lint claims brand roadmap secrets leak hooks fresh-check deploy-dry \
	demo demo-check demo-reset uki-cycle release-sign seed-cert

# `make check` is the CPU ceiling on this host: the sealed-image fixture drives
# `cryptsetup reencrypt` at about four cores, so two suites that overlap take
# the six vCPUs and a third makes load 13 (measured 2026-11-14). One host-wide
# lock serializes the gate; a suite started in another worktree takes a ticket
# and queues behind this one, in order, and prints why (tools/check_lock.py).
# Override CHECK_LOCK only to exercise the mechanism: a distinct path stops
# serializing the host (its queue is derived from it, `<lock>.queue`).
CHECK_LOCK ?= /var/lock/odile-check.lock
# The queue cap: about one full suite, measured at the shared lock as p50
# 744.7 s and p90 876.0 s over 84 holds on 2026-11-25/26. A caller still
# queued at 900 s gets one last lock attempt if it is the head, then exits 69
# naming its ticket, the live tickets ahead and the holder's elapsed hold —
# it is never killed at a caller's terminal cap with nothing to show.
CHECK_LOCK_WAIT ?= 900
# The wait journal: one JSON line per acquisition, with the exact wait, the
# holder pid and the outcome. `make check` announces a wait once and is silent
# until it takes the lock, so this is the only exact per-run wait there is.
# Empty disables it; tests pass their own path.
CHECK_LOCK_EVENT ?= /var/lock/odile-check-waits.jsonl
# `$(MAKE)` in a recipe is executed even under `make -n`, and would take the
# real lock during a dry run. An alias keeps the recursion but lets -n print.
MAKE_BIN := $(MAKE)
CHECK_LOCK_PY := $(PYTHON) tools/check_lock.py --lock "$(CHECK_LOCK)" --wait "$(CHECK_LOCK_WAIT)" --event-log "$(CHECK_LOCK_EVENT)" --

check:
	@$(CHECK_LOCK_PY) $(MAKE_BIN) --no-print-directory check-inner

# The fast gate `.githooks/pre-commit` runs. It is deliberately *not* under the
# host lock: the one heavy module (`tests/test_sealed_image.py`, cryptsetup
# re-encrypt at about four cores) is excluded, so a commit can neither queue
# behind a full suite nor hold the lock for 11 minutes. The full suite still
# runs at `make acceptance` and at the merge gate — the two points where a tree
# lands on `main` — and the trade is recorded in docs/ops/WORKFLOW-CHANGES.md.
# The test set is the modules the index touches (by basename), plus a smoke set
# that covers the app, the claims/brand linters and the lock/machinery code.
FAST_SMOKE ?= tests/test_flow.py tests/test_claims.py tests/test_brand.py tests/test_check_lock.py
FAST_HEAVY ?= tests/test_sealed_image.py
check-fast: prereqs secrets leak compile lint claims brand roadmap
	@$(MAKE_BIN) --no-print-directory test-fast

test-fast:
	@set -eu; \
	changed=$$( { $(GIT) diff --cached --name-only --diff-filter=ACMR; \
	              $(GIT) diff --name-only --diff-filter=ACMR; } | sort -u ); \
	mods=$$( \
	  for f in $$changed; do \
	    case "$$f" in \
	      tests/test_*.py) echo "$$f" ;; \
	      *.py) base=$$(basename "$$f" .py); \
	            if [ -f "tests/test_$$base.py" ]; then echo "tests/test_$$base.py"; fi ;; \
	    esac; \
	  done; \
	  for f in $(FAST_SMOKE); do if [ -f "$$f" ]; then echo "$$f"; fi; done; \
	); \
	mods=$$(printf '%s\n' "$$mods" | sort -u | sed '/^$$/d'); \
	run=""; skipped=""; heavy=" $(FAST_HEAVY) "; \
	for f in $$mods; do \
	  case "$$heavy" in *" $$f "*) skipped="$$skipped $$f" ;; *) run="$$run $$f" ;; esac; \
	done; \
	if [ -z "$$run" ]; then echo "check-fast: no test module to run" >&2; exit 1; fi; \
	names=$$(printf '%s\n' $$run | sed 's|/|.|g; s|\.py$$||'); \
	echo "check-fast: $$(printf '%s ' $$names)"; \
	$(PYTHON) -m unittest $$names; \
	if [ -n "$$skipped" ]; then \
	  echo "check-fast: heavy module(s) left to the full gate:$$skipped"; \
	fi

# The gate proper. Separate from `check` so the lock spans every step -- the
# light linters as well as `test` and `self-test` -- and two runs cannot
# interleave their halves on a six-vCPU host.
check-inner: prereqs secrets leak test self-test compile lint claims brand roadmap
	@echo "make check: OK"

# The handoff gate. A card runs this before it asks for review, so the verifier
# reviews an artifact instead of re-driving the build. CARD=ODL-472 adds the
# per-card checks: clean tree, merges into main, evidence present.
#
# `acceptance` does NOT run `check` in the card's worktree. The full suite runs
# once per acceptance, in `card_acceptance.py --full`, inside a throwaway
# checkout of `git merge-tree --write-tree main HEAD` — the tree that lands — and
# the stamp it writes names that tree (`gate: full-suite-on-merge-tree`). The
# merge gate reuses the stamp only on tree equality, so one acceptance is one
# full suite (t_123bbc46; `make check` stays the merge gate and the manual
# gate). `check-hq` still runs first: the HQ-only test modules against the
# branch's merge preview, where a card that was green in its worktree twice went
# red in main.
#
# Both tools below are private machinery at HQ (`.git/info/exclude`), so a card
# worktree carries no copy and a relative path dies there. Resolve each the way
# `tools/hq_gate.py` does: the tree's own file when it carries one, else the
# checkout a linked worktree shares — the parent of `git rev-parse
# --git-common-dir`. The resolved path is outside the card's checkout, so the
# gate still leaves `git status` clean.
HQ_COMMON := $(shell $(GIT) rev-parse --path-format=absolute --git-common-dir 2>/dev/null)
HQ_CHECKOUT := $(patsubst %/.git,%,$(HQ_COMMON))
acceptance_tool = $(if $(wildcard $(1)),$(1),$(if $(wildcard $(HQ_CHECKOUT)/$(1)),$(HQ_CHECKOUT)/$(1),$(1)))
BOARD_SYNC := $(call acceptance_tool,tools/board_sync.py)
CARD_ACCEPTANCE := $(call acceptance_tool,tools/card_acceptance.py)
acceptance: check-hq
	@$(PYTHON) "$(BOARD_SYNC)" check
	@if [ -n "$(CARD)" ]; then $(PYTHON) "$(CARD_ACCEPTANCE)" --card "$(CARD)" --full; fi
	@echo "make acceptance: OK $(CARD)"

# The HQ half of the suite (ODL-479). A card worktree is a fresh checkout, so
# `unittest discover -s tests` there finds only the tracked modules — 66 of the
# 82 `tests/test_*.py` at `main` `ef23edf`; the other 16 exist only at HQ
# (`.git/info/exclude`) and carry rules about the tree — every `docs/*.md`
# quoting the UKI `f7bc0ceb…` must name `t_c1e0f336`, every craft playbook
# keeps its five sections — so a card breaking one was green in its worktree and
# red for everyone in main. `tools/hq_gate.py` runs those modules against this
# checkout's *merge preview*: a throwaway worktree at the branch's commit with
# the paths the branch left alone taken as HQ's HEAD has them, main's deletions
# applied too, the HQ-only files overlaid, and a seat's staged-at-HQ batch read
# from HQ's index, removed afterwards, so the card's tree is never written to.
# A path main deleted that HQ still carries — ignored on disk, or staged in its
# index — is the exception: the preview keeps HQ's bytes and drops only the index
# entry, as HQ's own checkout has it.
# Outside HQ (a public clone) the tool prints one skip line and passes.
check-hq:
	@$(CHECK_LOCK_PY) $(PYTHON) tools/hq_gate.py

prereqs:
	@command -v "$(PYTHON)" >/dev/null 2>&1 || { echo "make check: $(PYTHON) is not executable (set PYTHON=...)" >&2; exit 1; }
	@command -v "$(SHELLCHECK)" >/dev/null 2>&1 || { echo "make check: $(SHELLCHECK) is not installed (apt-get install shellcheck)" >&2; exit 1; }
	@command -v openssl >/dev/null 2>&1 || { echo "make check: openssl is not installed (apt-get install openssl)" >&2; exit 1; }
# The Hetzner pair's key install is the one place `odilectl provision` shells
# out to OpenSSH: `ssh-keygen`/`ssh-keyscan` pin the host key and `ssh` over
# port 23 installs the public key, with the one-time password handed to
# `sshpass` through a 0600 file. A missing binary used to be an uncaught
# `FileNotFoundError` at the first install (ODL-924 review 1).
	@command -v ssh-keygen >/dev/null 2>&1 || { echo "make check: ssh-keygen is not installed (apt-get install openssh-client)" >&2; exit 1; }
	@command -v ssh-keyscan >/dev/null 2>&1 || { echo "make check: ssh-keyscan is not installed (apt-get install openssh-client)" >&2; exit 1; }
	@command -v sshpass >/dev/null 2>&1 || { echo "make check: sshpass is not installed (apt-get install sshpass)" >&2; exit 1; }

# One suite: every test lives under tests/, so `discover` sees all of them.
test: test-unit

test-unit:
	@$(CHECK_LOCK_PY) $(PYTHON) -m unittest discover -s tests -v

# The installer's own suite: it verifies the shipped bytes against the
# detached signature and, in a checkout, the embedded tenant files against
# their sources. Without this step a commit could edit the installer and stay
# green with a stale pair (ODL-403). The signature check shells out to openssl,
# which prereqs names.
#
# The demo deploy's suite is the only check of its pure functions (Caddy
# splicing, the DNS way-back line, the lenient YAML read) and of the tenant
# apply's own self-test: it failed for days with no gate running it, so it runs
# here too (ODL-901 retired the README table its first check read).
#
# The release verb's suite drives publish and the prune over a fixture tree.
# The prune deletes files from a served directory (the ODL-356 retention rule),
# so a silent regression there is not caught by any other step: it runs here too.
self-test: prereqs
	@$(CHECK_LOCK_PY) $(PYTHON) installer/odile_install.py --self-test
	@$(CHECK_LOCK_PY) $(PYTHON) deploy/demo-host/deploy_demo_public.py --self-test
	@$(CHECK_LOCK_PY) $(PYTHON) deploy/caddy/deploy_releases.py --self-test

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

# The publication gate: no address, mailbox, home path, account id, credential
# assignment or private literal in the tree. It runs in `check` as well as in CI
# (`publish-lint`), because `.githooks/pre-commit` runs `check` — a leak dies
# before it becomes a commit, which is the only point where the fix is cheap.
#
# There is one deny list, and it is HQ-only: `docs/ops/leak-forbid.txt` (the
# directory is excluded from git), installed in CI from the `LEAK_FORBID_B64`
# repository secret. The repository carries no copy, so a fresh clone has no
# list: this target prints `forbid list absent` and still runs the tree classes,
# rather than passing as if the class had run. That absent branch is the
# target's, whatever path is named — `make leak FORBID_FILE=/nonexistent` also
# prints the line and passes, because the target's job is the tree. Exit 2 is
# the scanner's own contract, not the target's: a direct call
# `tools/leak_scan.py --forbid-file /nonexistent` exits 2, because the caller
# named a file the scan must have.
FORBID_FILE ?= docs/ops/leak-forbid.txt
leak: prereqs
	@if [ -f "$(FORBID_FILE)" ]; then \
		$(PYTHON) tools/leak_scan.py --tree --forbid-file "$(FORBID_FILE)"; \
	else \
		printf 'leak: forbid list absent: %s — the forbidden-value and forbidden-path classes did not run\n' "$(FORBID_FILE)"; \
		printf 'leak: supply it at that path (HQ keeps it; CI reads the LEAK_FORBID_B64 secret)\n'; \
		$(PYTHON) tools/leak_scan.py --tree; \
	fi

# Enable the committed hooks in this clone. Idempotent: git keeps the path in
# local config, so run it once per clone. The commit-msg hook scans the message
# before the commit object exists; the pre-push hook scans the outgoing diff and
# commit messages against the same deny list. `git commit --no-verify` and
# `git push --no-verify` are the deliberate bypasses.
hooks:
	@git rev-parse --git-dir >/dev/null 2>&1 || { echo "make hooks: run inside a git checkout" >&2; exit 1; }
	@git config core.hooksPath .githooks
	@chmod +x .githooks/commit-msg .githooks/pre-commit .githooks/pre-push
	@echo "make hooks: commit-msg, pre-commit and pre-push leak gates enabled"
	@echo "make hooks: bypass with: git commit --no-verify / git push --no-verify"

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
