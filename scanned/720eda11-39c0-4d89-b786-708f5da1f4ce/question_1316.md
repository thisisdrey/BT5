# Q1316: account_not_frozen_for_authority: authority binding bypass on account state mutation [a-delegated-or-migrated-authority] [partial-transition]

## Question
Can an unprivileged attacker call `lending_account_withdraw` and make `account_not_frozen_for_authority` accept a delegated or migrated authority context on a frozen account so another user's account state mutates without valid authority, violating `freeze semantics must block every forbidden value-moving path for the affected authority and account` and leading to `High: unauthorized state mutation or bypass of safety freeze with financial effect`? Focus specifically on whether a partially completed state transition leaves enough residual state for the attacker to finish it incorrectly.

## Target
- File/function: `programs/marginfi/src/state/marginfi_account.rs` / `account_not_frozen_for_authority`
- Entrypoint: `lending_account_withdraw`
- Attacker controls: a delegated or migrated authority context on a frozen account
- Exploit idea: Probe signer checks, transferred authorities, PDA ownership, and migrated-account state so public calls cannot rewrite who controls a margin account. Focus specifically on whether a partially completed state transition leaves enough residual state for the attacker to finish it incorrectly.
- Invariant to test: freeze semantics must block every forbidden value-moving path for the affected authority and account
- Expected Immunefi impact: High: unauthorized state mutation or bypass of safety freeze with financial effect
- Fast validation: Use two users and assert the controlled call cannot mutate the victim account's owner, flags, balances, or closeability. Force failure after the first state mutation and assert the attacker cannot finalize, replay, or redirect the transition.
