# Q3583: close_account: account migration duplicates or strands value [an-account-with-historical-disabled] [role-reuse]

## Question
Can an unprivileged attacker use `close_account` with an account with historical disabled/bankrupted markers so `close_account` duplicates, drops, or strands balances during account migration or transfer, violating `closing an account must never strand value or release a container that still secures live positions` and causing `High: permanent lock or hidden exposure with real financial effect`? Focus specifically on stale authority, migrated-account, or delegated-account reuse after one valid-looking transition.

## Target
- File/function: `programs/marginfi/src/instructions/marginfi_account/close.rs` / `close_account`
- Entrypoint: `close_account`
- Attacker controls: an account with historical disabled/bankrupted markers
- Exploit idea: Probe migration edges where balances, fees, or authorities are copied then cleared, especially if one half can be replayed or partially completed. Focus specifically on stale authority, migrated-account, or delegated-account reuse after one valid-looking transition.
- Invariant to test: closing an account must never strand value or release a container that still secures live positions
- Expected Immunefi impact: High: permanent lock or hidden exposure with real financial effect
- Fast validation: Simulate partial completion and replay attempts, then assert total exposure across old and new accounts stays conserved. Model one valid transition first, then retry with the attacker as the next caller and assert authority cannot be reused.
