# Q5938: process-debt-asset via liquidate-multi: make two code sites that must agree disagree by an attacke

## Question
Entering through `liquidate-multi` (mainnet/contracts/market/v0-4-market.clar:1593) while controlling the trait principals supplied per entry, can an unprivileged attacker make `process-debt-asset` (mainnet/contracts/market/v0-4-market.clar:761) make two code sites that must agree disagree by an attacker-chosen amount? `process-debt-asset` caps debt at the max liquidatable USD and converts back to tokens with `mul-div-down`, so the invariant that a vault's underlying plus outstanding debt covers all shares and all supplier claims would fail, yielding theft of unclaimed yield.

## Target
- File/function: `mainnet/contracts/market/v0-4-market.clar:761` -> `process-debt-asset`
- Entrypoint: `liquidate-multi` (`mainnet/contracts/market/v0-4-market.clar:1593`), unprivileged and publicly callable
- Attacker controls: the trait principals supplied per entry
- Exploit idea: `process-debt-asset` caps debt at the max liquidatable USD and converts back to tokens with `mul-div-down`. Reach it through `liquidate-multi` and make two code sites that must agree disagree by an attacker-chosen amount.
- Invariant to test: a vault's underlying plus outstanding debt covers all shares and all supplier claims
- Expected Immunefi impact: High - theft of unclaimed yield
- Fast validation: Set up the position in simnet, call `liquidate-multi` with the trait principals supplied per entry, and assert on the printed event plus the post-state that collateral, debt and share totals still reconcile.
