# Q3878: lending_account_liquidate: liquidation start/end pair can settle inconsistent state [a-victim-exactly-at-the] [threshold]

## Question
Can an unprivileged attacker use `lending_account_liquidate` with a victim exactly at the liquidatable threshold under one fresh view so `lending_account_liquidate` leaves liquidation state inconsistent between start and end phases, violating `liquidation must repay real debt and seize only the allowed value from the correct victim balances` and causing `Critical: direct theft from victims or solvency loss via under-repayment`? Focus specifically on victim states that sit exactly on the fresh-liquidatable boundary under conservative pricing.

## Target
- File/function: `programs/marginfi/src/instructions/marginfi_account/liquidate.rs` / `lending_account_liquidate`
- Entrypoint: `lending_account_liquidate`
- Attacker controls: a victim exactly at the liquidatable threshold under one fresh view
- Exploit idea: Probe whether the multi-step liquidation state machine can be entered, exited, or replayed from mismatched account/bank context. Focus specifically on victim states that sit exactly on the fresh-liquidatable boundary under conservative pricing.
- Invariant to test: liquidation must repay real debt and seize only the allowed value from the correct victim balances
- Expected Immunefi impact: Critical: direct theft from victims or solvency loss via under-repayment
- Fast validation: Exercise start/end under the controlled mismatch and assert the account cannot exit with partially settled debt or duplicated seized assets. Sweep around the liquidation threshold and assert no branch accepts if a full fresh recomputation would reject.
