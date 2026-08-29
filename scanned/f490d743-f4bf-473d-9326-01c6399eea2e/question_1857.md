# Q1857: calc-liq-factor-exp via liquidate-multi: reach a state the guard immediately upstream of it never c

## Question
Can an unprivileged attacker entering through `liquidate-multi` (mainnet/contracts/market/v0-4-market.clar:1593), controlling how many entries share one price snapshot (price-feeds is passed as none), drive `calc-liq-factor-exp` (mainnet/contracts/market/v0-4-market.clar:708) — which uses `(/ exp BPS)` as an integer exponent for `pow` and falls back to `sqrti` below BPS — to reach a state the guard immediately upstream of it never contemplated, breaking the invariant that a vault's underlying plus outstanding debt covers all shares and all supplier claims, and cause permanent freezing of funds?

## Target
- File/function: `mainnet/contracts/market/v0-4-market.clar:708` -> `calc-liq-factor-exp`
- Entrypoint: `liquidate-multi` (`mainnet/contracts/market/v0-4-market.clar:1593`), unprivileged and publicly callable
- Attacker controls: how many entries share one price snapshot (price-feeds is passed as none)
- Exploit idea: `calc-liq-factor-exp` uses `(/ exp BPS)` as an integer exponent for `pow` and falls back to `sqrti` below BPS. Reach it through `liquidate-multi` and reach a state the guard immediately upstream of it never contemplated.
- Invariant to test: a vault's underlying plus outstanding debt covers all shares and all supplier claims
- Expected Immunefi impact: Critical - permanent freezing of funds
- Fast validation: Snapshot every state variable `calc-liq-factor-exp` touches, run `liquidate-multi` with how many entries share one price snapshot (price-feeds is passed as none), recompute the invariant off-chain from the snapshot, and assert it matches the on-chain result.
