# Q3987: lending_account_liquidate: zeroing or closing during liquidation loses obligations [remaining-accounts-that-present-multiple] [phase-replay]

## Question
Can an unprivileged attacker use `lending_account_liquidate` with remaining accounts that present multiple victim/liquidator bank pairings so `lending_account_liquidate` zeroes, closes, or reclassifies a balance during liquidation while debt/value still exists, breaking `liquidation must repay real debt and seize only the allowed value from the correct victim balances` and causing `Critical: direct theft from victims or solvency loss via under-repayment`? Focus specifically on replay or reorder of multi-phase liquidation state after one successful-looking intermediate step.

## Target
- File/function: `programs/marginfi/src/instructions/marginfi_account/liquidate.rs` / `lending_account_liquidate`
- Entrypoint: `lending_account_liquidate`
- Attacker controls: remaining accounts that present multiple victim/liquidator bank pairings
- Exploit idea: Inspect edge transitions near tiny balances and dust thresholds where the state machine may decide a leg is finished too early. Focus specifically on replay or reorder of multi-phase liquidation state after one successful-looking intermediate step.
- Invariant to test: liquidation must repay real debt and seize only the allowed value from the correct victim balances
- Expected Immunefi impact: Critical: direct theft from victims or solvency loss via under-repayment
- Fast validation: Run liquidation at boundary values and assert no balance is closed or marked inactive while any economic exposure remains. Execute start/end phases with replay and reorder attempts and assert no extra seize, skipped repay, or stuck flag survives.
