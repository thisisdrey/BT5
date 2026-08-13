# Q1398: account_not_frozen_for_authority: account migration duplicates or strands value [a-same-slot-sequence-that] [partial-transition]

## Question
Can an unprivileged attacker use `lending_account_withdraw` with a same-slot sequence that freezes and then attempts a value-moving action so `account_not_frozen_for_authority` duplicates, drops, or strands balances during account migration or transfer, violating `freeze semantics must block every forbidden value-moving path for the affected authority and account` and causing `High: unauthorized state mutation or bypass of safety freeze with financial effect`? Focus specifically on whether a partially completed state transition leaves enough residual state for the attacker to finish it incorrectly.

## Target
- File/function: `programs/marginfi/src/state/marginfi_account.rs` / `account_not_frozen_for_authority`
- Entrypoint: `lending_account_withdraw`
- Attacker controls: a same-slot sequence that freezes and then attempts a value-moving action
- Exploit idea: Probe migration edges where balances, fees, or authorities are copied then cleared, especially if one half can be replayed or partially completed. Focus specifically on whether a partially completed state transition leaves enough residual state for the attacker to finish it incorrectly.
- Invariant to test: freeze semantics must block every forbidden value-moving path for the affected authority and account
- Expected Immunefi impact: High: unauthorized state mutation or bypass of safety freeze with financial effect
- Fast validation: Simulate partial completion and replay attempts, then assert total exposure across old and new accounts stays conserved. Force failure after the first state mutation and assert the attacker cannot finalize, replay, or redirect the transition.
