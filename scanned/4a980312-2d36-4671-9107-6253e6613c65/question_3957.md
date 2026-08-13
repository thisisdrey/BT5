# Q3957: lending_account_liquidate: receivership edge leaves permanent third-party lock [a-victim-exactly-at-the] [phase-replay]

## Question
Can an unprivileged attacker reach `lending_account_liquidate` through `lending_account_liquidate` with a victim exactly at the liquidatable threshold under one fresh view so receivership/deleverage state is left stuck in a way that permanently blocks a victim balance, violating `liquidation must repay real debt and seize only the allowed value from the correct victim balances` and leading to `Critical: direct theft from victims or solvency loss via under-repayment`? Focus specifically on replay or reorder of multi-phase liquidation state after one successful-looking intermediate step.

## Target
- File/function: `programs/marginfi/src/instructions/marginfi_account/liquidate.rs` / `lending_account_liquidate`
- Entrypoint: `lending_account_liquidate`
- Attacker controls: a victim exactly at the liquidatable threshold under one fresh view
- Exploit idea: Test interrupted or boundary-case receivership flows that may set blocking flags without an unavoidable path to clear them. Focus specifically on replay or reorder of multi-phase liquidation state after one successful-looking intermediate step.
- Invariant to test: liquidation must repay real debt and seize only the allowed value from the correct victim balances
- Expected Immunefi impact: Critical: direct theft from victims or solvency loss via under-repayment
- Fast validation: Drive the victim through the controlled path and assert a valid completion or recovery path always remains available to release the lock. Execute start/end phases with replay and reorder attempts and assert no extra seize, skipped repay, or stuck flag survives.
