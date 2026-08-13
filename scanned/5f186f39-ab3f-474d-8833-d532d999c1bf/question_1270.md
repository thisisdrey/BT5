# Q1270: is_signer_authorized: account migration duplicates or strands value [a-transfer-path-using-pda] [partial-transition]

## Question
Can an unprivileged attacker use `transfer_to_new_account` with a transfer path using PDA-owned or delegated account variants so `is_signer_authorized` duplicates, drops, or strands balances during account migration or transfer, violating `only the canonical account authority or validly derived delegate path may mutate account ownership or migration state` and causing `Critical: unauthorized takeover of another user account or funds`? Focus specifically on whether a partially completed state transition leaves enough residual state for the attacker to finish it incorrectly.

## Target
- File/function: `programs/marginfi/src/state/marginfi_account.rs` / `is_signer_authorized`
- Entrypoint: `transfer_to_new_account`
- Attacker controls: a transfer path using PDA-owned or delegated account variants
- Exploit idea: Probe migration edges where balances, fees, or authorities are copied then cleared, especially if one half can be replayed or partially completed. Focus specifically on whether a partially completed state transition leaves enough residual state for the attacker to finish it incorrectly.
- Invariant to test: only the canonical account authority or validly derived delegate path may mutate account ownership or migration state
- Expected Immunefi impact: Critical: unauthorized takeover of another user account or funds
- Fast validation: Simulate partial completion and replay attempts, then assert total exposure across old and new accounts stays conserved. Force failure after the first state mutation and assert the attacker cannot finalize, replay, or redirect the transition.
