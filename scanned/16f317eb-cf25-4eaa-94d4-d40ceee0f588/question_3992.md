# Q3992: lending_account_liquidate: zeroing or closing during liquidation loses obligations [a-same-slot-price-cache] [threshold]

## Question
Can an unprivileged attacker use `lending_account_liquidate` with a same-slot price-cache or health-cache change before liquidation so `lending_account_liquidate` zeroes, closes, or reclassifies a balance during liquidation while debt/value still exists, breaking `liquidation must repay real debt and seize only the allowed value from the correct victim balances` and causing `Critical: direct theft from victims or solvency loss via under-repayment`? Focus specifically on victim states that sit exactly on the fresh-liquidatable boundary under conservative pricing.

## Target
- File/function: `programs/marginfi/src/instructions/marginfi_account/liquidate.rs` / `lending_account_liquidate`
- Entrypoint: `lending_account_liquidate`
- Attacker controls: a same-slot price-cache or health-cache change before liquidation
- Exploit idea: Inspect edge transitions near tiny balances and dust thresholds where the state machine may decide a leg is finished too early. Focus specifically on victim states that sit exactly on the fresh-liquidatable boundary under conservative pricing.
- Invariant to test: liquidation must repay real debt and seize only the allowed value from the correct victim balances
- Expected Immunefi impact: Critical: direct theft from victims or solvency loss via under-repayment
- Fast validation: Run liquidation at boundary values and assert no balance is closed or marked inactive while any economic exposure remains. Sweep around the liquidation threshold and assert no branch accepts if a full fresh recomputation would reject.
