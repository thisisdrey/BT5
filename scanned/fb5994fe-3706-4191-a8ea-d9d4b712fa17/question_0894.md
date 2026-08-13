# Q894: initialize: account migration duplicates or strands value [an-init-call-where-the] [partial-transition]

## Question
Can an unprivileged attacker use `initialize_account` with an init call where the target account key has historical state from another workflow so `initialize` duplicates, drops, or strands balances during account migration or transfer, violating `new account initialization must bind authority, group, and flags canonically and never let a stranger create a controllable victim context` and causing `High: unauthorized state change or durable victim fund freeze`? Focus specifically on whether a partially completed state transition leaves enough residual state for the attacker to finish it incorrectly.

## Target
- File/function: `programs/marginfi/src/state/marginfi_account.rs` / `initialize`
- Entrypoint: `initialize_account`
- Attacker controls: an init call where the target account key has historical state from another workflow
- Exploit idea: Probe migration edges where balances, fees, or authorities are copied then cleared, especially if one half can be replayed or partially completed. Focus specifically on whether a partially completed state transition leaves enough residual state for the attacker to finish it incorrectly.
- Invariant to test: new account initialization must bind authority, group, and flags canonically and never let a stranger create a controllable victim context
- Expected Immunefi impact: High: unauthorized state change or durable victim fund freeze
- Fast validation: Simulate partial completion and replay attempts, then assert total exposure across old and new accounts stays conserved. Force failure after the first state mutation and assert the attacker cannot finalize, replay, or redirect the transition.
