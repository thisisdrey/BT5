# Q2973: interpolate-rate via collateral-remove: make a health check read a different position than the one

## Question
Can an unprivileged attacker entering through `collateral-remove` (mainnet/contracts/market/v0-4-market.clar:1107), controlling the set of assets held, drive `interpolate-rate` (mainnet/contracts/vault/v0-vault-stx.clar:196) — which interpolates between packed u16 curve points — to make a health check read a different position than the one that will exist, breaking the invariant that every asset a position holds enters the health evaluation exactly once, and cause permanent freezing of funds?

## Target
- File/function: `mainnet/contracts/vault/v0-vault-stx.clar:196` -> `interpolate-rate`
- Entrypoint: `collateral-remove` (`mainnet/contracts/market/v0-4-market.clar:1107`), unprivileged and publicly callable
- Attacker controls: the set of assets held
- Exploit idea: `interpolate-rate` interpolates between packed u16 curve points. Reach it through `collateral-remove` and make a health check read a different position than the one that will exist.
- Invariant to test: every asset a position holds enters the health evaluation exactly once
- Expected Immunefi impact: Critical - permanent freezing of funds
- Fast validation: Snapshot every state variable `interpolate-rate` touches, run `collateral-remove` with the set of assets held, recompute the invariant off-chain from the snapshot, and assert it matches the on-chain result.
