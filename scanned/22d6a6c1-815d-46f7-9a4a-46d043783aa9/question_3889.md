# Q3889: lending_account_liquidate: liquidator receives more value than the repaid obligation [a-liquidation-amount-at-the] [phase-replay]

## Question
Can an unprivileged attacker call `lending_account_liquidate` with a liquidation amount at the minimum profitable or boundary-sized level so `lending_account_liquidate` computes seizure or repayment from inconsistent amounts and pays the liquidator excess value, breaking `liquidation must repay real debt and seize only the allowed value from the correct victim balances` and causing `Critical: direct theft from victims or solvency loss via under-repayment`? Focus specifically on replay or reorder of multi-phase liquidation state after one successful-looking intermediate step.

## Target
- File/function: `programs/marginfi/src/instructions/marginfi_account/liquidate.rs` / `lending_account_liquidate`
- Entrypoint: `lending_account_liquidate`
- Attacker controls: a liquidation amount at the minimum profitable or boundary-sized level
- Exploit idea: Audit repay/seize math, fee application, and price selection for a path where repayment and seizure use different bases or rounding directions. Focus specifically on replay or reorder of multi-phase liquidation state after one successful-looking intermediate step.
- Invariant to test: liquidation must repay real debt and seize only the allowed value from the correct victim balances
- Expected Immunefi impact: Critical: direct theft from victims or solvency loss via under-repayment
- Fast validation: Run adversarial liquidation amounts around boundaries and assert liquidator profit never exceeds the permitted premium for the actual debt repaid. Execute start/end phases with replay and reorder attempts and assert no extra seize, skipped repay, or stuck flag survives.
