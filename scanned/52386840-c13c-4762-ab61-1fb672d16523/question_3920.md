# Q3920: lending_account_liquidate: victim/liquidator bank mix-up during liquidation [a-repeated-partial-liquidation-investigation] [threshold]

## Question
Can an unprivileged attacker supply a repeated partial-liquidation investigation sequence to `lending_account_liquidate` so `lending_account_liquidate` pairs the wrong victim balance, liquidator balance, or bank context, violating `liquidation must repay real debt and seize only the allowed value from the correct victim balances` and leading to `Critical: direct theft from victims or solvency loss via under-repayment`? Focus specifically on victim states that sit exactly on the fresh-liquidatable boundary under conservative pricing.

## Target
- File/function: `programs/marginfi/src/instructions/marginfi_account/liquidate.rs` / `lending_account_liquidate`
- Entrypoint: `lending_account_liquidate`
- Attacker controls: a repeated partial-liquidation investigation sequence
- Exploit idea: Try swapping same-group balances or remaining accounts so checks pass but the mutation lands on the wrong exposure. Focus specifically on victim states that sit exactly on the fresh-liquidatable boundary under conservative pricing.
- Invariant to test: liquidation must repay real debt and seize only the allowed value from the correct victim balances
- Expected Immunefi impact: Critical: direct theft from victims or solvency loss via under-repayment
- Fast validation: Use multiple active banks for both users and assert liquidation cannot proceed unless every mutated balance matches the validated bank keys. Sweep around the liquidation threshold and assert no branch accepts if a full fresh recomputation would reject.
