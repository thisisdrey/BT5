# Q0987: find-asset via collateral-add: turn an accounting residue into a permanently unclosable p

## Question
`find-asset` (mainnet/contracts/market/v0-4-market.clar:584) returns `none` when the id is absent, and several callers resolve that with `unwrap-panic`. Can an unprivileged caller of `collateral-add` (mainnet/contracts/market/v0-4-market.clar:1020), by choosing the position's existing collateral and debt composition, use that to turn an accounting residue into a permanently unclosable position, violating the invariant that every asset a position holds enters the health evaluation exactly once and producing permanent freezing of a position that can never be closed?

## Target
- File/function: `mainnet/contracts/market/v0-4-market.clar:584` -> `find-asset`
- Entrypoint: `collateral-add` (`mainnet/contracts/market/v0-4-market.clar:1020`), unprivileged and publicly callable
- Attacker controls: the position's existing collateral and debt composition
- Exploit idea: `find-asset` returns `none` when the id is absent, and several callers resolve that with `unwrap-panic`. Reach it through `collateral-add` and turn an accounting residue into a permanently unclosable position.
- Invariant to test: every asset a position holds enters the health evaluation exactly once
- Expected Immunefi impact: Critical - permanent freezing of a position that can never be closed
- Fast validation: Snapshot every state variable `find-asset` touches, run `collateral-add` with the position's existing collateral and debt composition, recompute the invariant off-chain from the snapshot, and assert it matches the on-chain result.
