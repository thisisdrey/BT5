# Q1186: is_signer_authorized: authority binding bypass on account state mutation [an-attacker-signer-paired-with] [partial-transition]

## Question
Can an unprivileged attacker call `transfer_to_new_account` and make `is_signer_authorized` accept an attacker signer paired with a victim account and attacker-chosen auxiliary accounts so another user's account state mutates without valid authority, violating `only the canonical account authority or validly derived delegate path may mutate account ownership or migration state` and leading to `Critical: unauthorized takeover of another user account or funds`? Focus specifically on whether a partially completed state transition leaves enough residual state for the attacker to finish it incorrectly.

## Target
- File/function: `programs/marginfi/src/state/marginfi_account.rs` / `is_signer_authorized`
- Entrypoint: `transfer_to_new_account`
- Attacker controls: an attacker signer paired with a victim account and attacker-chosen auxiliary accounts
- Exploit idea: Probe signer checks, transferred authorities, PDA ownership, and migrated-account state so public calls cannot rewrite who controls a margin account. Focus specifically on whether a partially completed state transition leaves enough residual state for the attacker to finish it incorrectly.
- Invariant to test: only the canonical account authority or validly derived delegate path may mutate account ownership or migration state
- Expected Immunefi impact: Critical: unauthorized takeover of another user account or funds
- Fast validation: Use two users and assert the controlled call cannot mutate the victim account's owner, flags, balances, or closeability. Force failure after the first state mutation and assert the attacker cannot finalize, replay, or redirect the transition.
