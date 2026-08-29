# Q3272: calc-liq-debt-repay via liquidate-multi: reach a state the guard immediately upstream of it never c

## Question
Does `liquidate-multi` (mainnet/contracts/market/v0-4-market.clar:1593) let an unprivileged attacker who controls which borrowers are placed early versus late in the batch reach `calc-liq-debt-repay` (mainnet/contracts/market/v0-4-market.clar:723) in a state where it reach a state the guard immediately upstream of it never contemplated? Given that it takes the liquidation factor times the debt with `mul-bps-down`, the invariant that only the acting principal's own position is mutated breaks and the result is protocol insolvency through uncollateralised debt.

## Target
- File/function: `mainnet/contracts/market/v0-4-market.clar:723` -> `calc-liq-debt-repay`
- Entrypoint: `liquidate-multi` (`mainnet/contracts/market/v0-4-market.clar:1593`), unprivileged and publicly callable
- Attacker controls: which borrowers are placed early versus late in the batch
- Exploit idea: `calc-liq-debt-repay` takes the liquidation factor times the debt with `mul-bps-down`. Reach it through `liquidate-multi` and reach a state the guard immediately upstream of it never contemplated.
- Invariant to test: only the acting principal's own position is mutated
- Expected Immunefi impact: Critical - protocol insolvency through uncollateralised debt
- Fast validation: Write a Clarinet simnet test calling `liquidate-multi` twice with which borrowers are placed early versus late in the batch varied, and assert that the value `calc-liq-debt-repay` returns is identical in both runs; a divergence confirms the finding.
