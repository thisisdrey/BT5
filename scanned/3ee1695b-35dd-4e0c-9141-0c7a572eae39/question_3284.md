# Q3284: lending_account_repay: cross-mode collateral view mismatch [a-same-slot-borrow-then] [cycle]

## Question
Can an unprivileged attacker use `lending_account_repay` with a same-slot borrow then repay sequence around rounding thresholds so `lending_account_repay` evaluates account risk under one mode and settles value under another, violating `repay must burn exactly the debt it settles and cannot erase liabilities without equivalent economic repayment` and resulting in `High: understated debt enabling later unauthorized withdrawal or protocol loss`? Focus specifically on whether the attacker can repeat the same path in a short deterministic cycle to compound a one-step drift.

## Target
- File/function: `programs/marginfi/src/instructions/marginfi_account/repay.rs` / `lending_account_repay`
- Entrypoint: `lending_account_repay`
- Attacker controls: a same-slot borrow then repay sequence around rounding thresholds
- Exploit idea: Probe transitions involving eMode, isolated assets, or asset tags where one code path reads stale or differently weighted collateral than the mutating path settles. Focus specifically on whether the attacker can repeat the same path in a short deterministic cycle to compound a one-step drift.
- Invariant to test: repay must burn exactly the debt it settles and cannot erase liabilities without equivalent economic repayment
- Expected Immunefi impact: High: understated debt enabling later unauthorized withdrawal or protocol loss
- Fast validation: Create a user that changes the relevant mode/context around the call and assert the instruction cannot accept if recomputation under a single consistent mode would fail. Run the target path in a short deposit/withdraw or borrow/repay style loop and assert no monotonic gain appears.
