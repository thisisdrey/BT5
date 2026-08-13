# Q3954: lending_account_liquidate: receivership edge leaves permanent third-party lock [a-liquidation-amount-at-the] [threshold]

## Question
Can an unprivileged attacker reach `lending_account_liquidate` through `lending_account_liquidate` with a liquidation amount at the minimum profitable or boundary-sized level so receivership/deleverage state is left stuck in a way that permanently blocks a victim balance, violating `liquidation must repay real debt and seize only the allowed value from the correct victim balances` and leading to `Critical: direct theft from victims or solvency loss via under-repayment`? Focus specifically on victim states that sit exactly on the fresh-liquidatable boundary under conservative pricing.

## Target
- File/function: `programs/marginfi/src/instructions/marginfi_account/liquidate.rs` / `lending_account_liquidate`
- Entrypoint: `lending_account_liquidate`
- Attacker controls: a liquidation amount at the minimum profitable or boundary-sized level
- Exploit idea: Test interrupted or boundary-case receivership flows that may set blocking flags without an unavoidable path to clear them. Focus specifically on victim states that sit exactly on the fresh-liquidatable boundary under conservative pricing.
- Invariant to test: liquidation must repay real debt and seize only the allowed value from the correct victim balances
- Expected Immunefi impact: Critical: direct theft from victims or solvency loss via under-repayment
- Fast validation: Drive the victim through the controlled path and assert a valid completion or recovery path always remains available to release the lock. Sweep around the liquidation threshold and assert no branch accepts if a full fresh recomputation would reject.
