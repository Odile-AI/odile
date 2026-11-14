Welcome. This is Odile's tenant machine: one shared workspace on our measured AMD
SEV-SNP machine. Odile holds this machine's boot key and can log in. Every judge
sees these sessions and files, and there is no nightly reset.

Try the one thing that proves the product:

1. Ask me: "Write a note in notes/ called hello.md and show me the file."
   Then open Artifacts → Files. Close the tab and come back: it is still here.
2. Ask me: "Run whoami and date." The shell is real; it runs as tenant.
3. Open https://demo.odile.fyi/verify — this machine's own access report, and
   the CPU's report behind it.

Inference runs on NEAR AI's confidential cloud, inside a hardware enclave. Your
prompts go from the browser to this machine and then to the enclave; the key
that pays for them stays on this machine.
