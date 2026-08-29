# Q5926: resolve-pyth via liquidate-multi: make two code sites that must agree disagree by an attacke

## Question
Entering through `liquidate-multi` (mainnet/contracts/market/v0-4-market.clar:1593) while controlling the trait principals supplied per entry, can an unprivileged attacker make `resolve-pyth` (mainnet/contracts/market/v0-4-market.clar:312) make two code sites that must agree disagree by an attacker-chosen amount? `resolve-pyth` reads the Pyth storage record for a 32-byte ident, so the invariant that no position row exists that the position mask does not represent would fail, yielding direct theft of another user's collateral.

## Target
- File/function: `mainnet/contracts/market/v0-4-market.clar:312` -> `resolve-pyth`
- Entrypoint: `liquidate-multi` (`mainnet/contracts/market/v0-4-market.clar:1593`), unprivileged and publicly callable
- Attacker controls: the trait principals supplied per entry
- Exploit idea: `resolve-pyth` reads the Pyth storage record for a 32-byte ident. Reach it through `liquidate-multi` and make two code sites that must agree disagree by an attacker-chosen amount.
- Invariant to test: no position row exists that the position mask does not represent
- Expected Immunefi impact: Critical - direct theft of another user's collateral
- Fast validation: Set up the position in simnet, call `liquidate-multi` with the trait principals supplied per entry, and assert on the printed event plus the post-state that collateral, debt and share totals still reconcile.
