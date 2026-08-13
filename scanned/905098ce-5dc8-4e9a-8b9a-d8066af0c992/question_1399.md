# Q1399: account_not_frozen_for_authority: account migration duplicates or strands value [an-account-where-only-part] [role-reuse]

## Question
Can an unprivileged attacker use `lending_account_withdraw` with an account where only part of the state machine reflects the frozen condition so `account_not_frozen_for_authority` duplicates, drops, or strands balances during account migration or transfer, violating `freeze semantics must block every forbidden value-moving path for the affected authority and account` and causing `High: unauthorized state mutation or bypass of safety freeze with financial effect`? Focus specifically on stale authority, migrated-account, or delegated-account reuse after one valid-looking transition.

## Target
- File/function: `programs/marginfi/src/state/marginfi_account.rs` / `account_not_frozen_for_authority`
- Entrypoint: `lending_account_withdraw`
- Attacker controls: an account where only part of the state machine reflects the frozen condition
- Exploit idea: Probe migration edges where balances, fees, or authorities are copied then cleared, especially if one half can be replayed or partially completed. Focus specifically on stale authority, migrated-account, or delegated-account reuse after one valid-looking transition.
- Invariant to test: freeze semantics must block every forbidden value-moving path for the affected authority and account
- Expected Immunefi impact: High: unauthorized state mutation or bypass of safety freeze with financial effect
- Fast validation: Simulate partial completion and replay attempts, then assert total exposure across old and new accounts stays conserved. Model one valid transition first, then retry with the attacker as the next caller and assert authority cannot be reused.
