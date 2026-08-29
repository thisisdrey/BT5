# Q3754: collateral-remove via transfer: turn an accounting residue into a permanently unclosable p

## Question
Entering through `transfer` (mainnet/contracts/vault/v0-vault-stx.clar:752) while controlling the destination principal, including the market, the market-vault or the treasury, can an unprivileged attacker make `collateral-remove` (mainnet/contracts/market/v0-market-vault.clar:406) turn an accounting residue into a permanently unclosable position? `collateral-remove` decrements the map and writes the entry before `send-tokens` executes, so the invariant that a value cached within a block still describes the state it was derived from would fail, yielding direct theft of user funds at rest or in motion.

## Target
- File/function: `mainnet/contracts/market/v0-market-vault.clar:406` -> `collateral-remove`
- Entrypoint: `transfer` (`mainnet/contracts/vault/v0-vault-stx.clar:752`), unprivileged and publicly callable
- Attacker controls: the destination principal, including the market, the market-vault or the treasury
- Exploit idea: `collateral-remove` decrements the map and writes the entry before `send-tokens` executes. Reach it through `transfer` and turn an accounting residue into a permanently unclosable position.
- Invariant to test: a value cached within a block still describes the state it was derived from
- Expected Immunefi impact: Critical - direct theft of user funds at rest or in motion
- Fast validation: Set up the position in simnet, call `transfer` with the destination principal, including the market, the market-vault or the treasury, and assert on the printed event plus the post-state that collateral, debt and share totals still reconcile.
