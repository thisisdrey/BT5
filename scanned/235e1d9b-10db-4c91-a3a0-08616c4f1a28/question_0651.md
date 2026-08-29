# Q0651: is-liquidation-paused via liquidate: compose two individually correct mechanisms into an incorr

## Question
`is-liquidation-paused` (mainnet/contracts/market/v0-4-market.clar:691) returns true if the manual pause, the GLOBAL grace entry, OR the per-asset grace entry is live. Can an unprivileged caller of `liquidate` (mainnet/contracts/market/v0-4-market.clar:1382), by choosing `min-collateral-expected`, use that to compose two individually correct mechanisms into an incorrect result, violating the invariant that a value cached within a block still describes the state it was derived from and producing permanent freezing of funds?

## Target
- File/function: `mainnet/contracts/market/v0-4-market.clar:691` -> `is-liquidation-paused`
- Entrypoint: `liquidate` (`mainnet/contracts/market/v0-4-market.clar:1382`), unprivileged and publicly callable
- Attacker controls: `min-collateral-expected`
- Exploit idea: `is-liquidation-paused` returns true if the manual pause, the GLOBAL grace entry, OR the per-asset grace entry is live. Reach it through `liquidate` and compose two individually correct mechanisms into an incorrect result.
- Invariant to test: a value cached within a block still describes the state it was derived from
- Expected Immunefi impact: Critical - permanent freezing of funds
- Fast validation: Snapshot every state variable `is-liquidation-paused` touches, run `liquidate` with `min-collateral-expected`, recompute the invariant off-chain from the snapshot, and assert it matches the on-chain result.
