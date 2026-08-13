# Q3500: close_account: authority binding bypass on account state mutation [an-account-that-just-exited] [partial-transition]

## Question
Can an unprivileged attacker call `close_account` and make `close_account` accept an account that just exited a flashloan or receivership state so another user's account state mutates without valid authority, violating `closing an account must never strand value or release a container that still secures live positions` and leading to `High: permanent lock or hidden exposure with real financial effect`? Focus specifically on whether a partially completed state transition leaves enough residual state for the attacker to finish it incorrectly.

## Target
- File/function: `programs/marginfi/src/instructions/marginfi_account/close.rs` / `close_account`
- Entrypoint: `close_account`
- Attacker controls: an account that just exited a flashloan or receivership state
- Exploit idea: Probe signer checks, transferred authorities, PDA ownership, and migrated-account state so public calls cannot rewrite who controls a margin account. Focus specifically on whether a partially completed state transition leaves enough residual state for the attacker to finish it incorrectly.
- Invariant to test: closing an account must never strand value or release a container that still secures live positions
- Expected Immunefi impact: High: permanent lock or hidden exposure with real financial effect
- Fast validation: Use two users and assert the controlled call cannot mutate the victim account's owner, flags, balances, or closeability. Force failure after the first state mutation and assert the attacker cannot finalize, replay, or redirect the transition.
