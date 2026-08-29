# Q4984: get-available-assets via deposit: make a health check read a different position than the one

## Question
Does `deposit` (mainnet/contracts/vault/v0-vault-stx.clar:763) let an unprivileged attacker who controls the vault's supply and asset state at the moment of the call reach `get-available-assets` (mainnet/contracts/vault/v0-vault-stx.clar:481) in a state where it make a health check read a different position than the one that will exist? Given that it reads real liquidity, a different quantity from the `assets` var that `redeem` also gates on, the invariant that conversions never round in the user's favour in either direction breaks and the result is direct theft of another user's collateral.

## Target
- File/function: `mainnet/contracts/vault/v0-vault-stx.clar:481` -> `get-available-assets`
- Entrypoint: `deposit` (`mainnet/contracts/vault/v0-vault-stx.clar:763`), unprivileged and publicly callable
- Attacker controls: the vault's supply and asset state at the moment of the call
- Exploit idea: `get-available-assets` reads real liquidity, a different quantity from the `assets` var that `redeem` also gates on. Reach it through `deposit` and make a health check read a different position than the one that will exist.
- Invariant to test: conversions never round in the user's favour in either direction
- Expected Immunefi impact: Critical - direct theft of another user's collateral
- Fast validation: Set up the position in simnet, call `deposit` with the vault's supply and asset state at the moment of the call, and assert on the printed event plus the post-state that collateral, debt and share totals still reconcile.
