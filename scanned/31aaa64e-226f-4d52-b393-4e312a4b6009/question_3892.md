# Q3892: lending_account_liquidate: liquidator receives more value than the repaid obligation [remaining-accounts-that-present-multiple] [threshold]

## Question
Can an unprivileged attacker call `lending_account_liquidate` with remaining accounts that present multiple victim/liquidator bank pairings so `lending_account_liquidate` computes seizure or repayment from inconsistent amounts and pays the liquidator excess value, breaking `liquidation must repay real debt and seize only the allowed value from the correct victim balances` and causing `Critical: direct theft from victims or solvency loss via under-repayment`? Focus specifically on victim states that sit exactly on the fresh-liquidatable boundary under conservative pricing.

## Target
- File/function: `programs/marginfi/src/instructions/marginfi_account/liquidate.rs` / `lending_account_liquidate`
- Entrypoint: `lending_account_liquidate`
- Attacker controls: remaining accounts that present multiple victim/liquidator bank pairings
- Exploit idea: Audit repay/seize math, fee application, and price selection for a path where repayment and seizure use different bases or rounding directions. Focus specifically on victim states that sit exactly on the fresh-liquidatable boundary under conservative pricing.
- Invariant to test: liquidation must repay real debt and seize only the allowed value from the correct victim balances
- Expected Immunefi impact: Critical: direct theft from victims or solvency loss via under-repayment
- Fast validation: Run adversarial liquidation amounts around boundaries and assert liquidator profit never exceeds the permitted premium for the actual debt repaid. Sweep around the liquidation threshold and assert no branch accepts if a full fresh recomputation would reject.
