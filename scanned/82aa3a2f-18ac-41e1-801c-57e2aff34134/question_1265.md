# Q1265: is_signer_authorized: account migration duplicates or strands value [an-attacker-signer-paired-with] [role-reuse]

## Question
Can an unprivileged attacker use `transfer_to_new_account` with an attacker signer paired with a victim account and attacker-chosen auxiliary accounts so `is_signer_authorized` duplicates, drops, or strands balances during account migration or transfer, violating `only the canonical account authority or validly derived delegate path may mutate account ownership or migration state` and causing `Critical: unauthorized takeover of another user account or funds`? Focus specifically on stale authority, migrated-account, or delegated-account reuse after one valid-looking transition.

## Target
- File/function: `programs/marginfi/src/state/marginfi_account.rs` / `is_signer_authorized`
- Entrypoint: `transfer_to_new_account`
- Attacker controls: an attacker signer paired with a victim account and attacker-chosen auxiliary accounts
- Exploit idea: Probe migration edges where balances, fees, or authorities are copied then cleared, especially if one half can be replayed or partially completed. Focus specifically on stale authority, migrated-account, or delegated-account reuse after one valid-looking transition.
- Invariant to test: only the canonical account authority or validly derived delegate path may mutate account ownership or migration state
- Expected Immunefi impact: Critical: unauthorized takeover of another user account or funds
- Fast validation: Simulate partial completion and replay attempts, then assert total exposure across old and new accounts stays conserved. Model one valid transition first, then retry with the attacker as the next caller and assert authority cannot be reused.
