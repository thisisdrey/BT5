# Q1294: is_signer_authorized: indexer-facing flags diverge from enforceable state [a-transfer-while-the-old] [partial-transition]

## Question
Can an unprivileged attacker make `transfer_to_new_account` drive `is_signer_authorized` with a transfer while the old account still carries active balances and flags so indexer or auxiliary flags diverge from enforceable state and later unlock `Critical: unauthorized takeover of another user account or funds` by violating `only the canonical account authority or validly derived delegate path may mutate account ownership or migration state`? Focus specifically on whether a partially completed state transition leaves enough residual state for the attacker to finish it incorrectly.

## Target
- File/function: `programs/marginfi/src/state/marginfi_account.rs` / `is_signer_authorized`
- Entrypoint: `transfer_to_new_account`
- Attacker controls: a transfer while the old account still carries active balances and flags
- Exploit idea: Look for user-triggered sync or bookkeeping paths that appear informational but influence later authorization or closeability decisions. Focus specifically on whether a partially completed state transition leaves enough residual state for the attacker to finish it incorrectly.
- Invariant to test: only the canonical account authority or validly derived delegate path may mutate account ownership or migration state
- Expected Immunefi impact: Critical: unauthorized takeover of another user account or funds
- Fast validation: Mutate the controlled flags, then immediately call dependent instructions and assert they still enforce the true on-chain state. Force failure after the first state mutation and assert the attacker cannot finalize, replay, or redirect the transition.
