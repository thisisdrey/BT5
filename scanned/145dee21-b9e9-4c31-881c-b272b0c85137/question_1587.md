# Q1587: accrue-debt-asset via collateral-remove: turn an accounting residue into a permanently unclosable p

## Question
`accrue-debt-asset` (mainnet/contracts/market/v0-4-market.clar:262) calls `accrue-and-cache` with `unwrap-panic` inside a fold whose accumulator ignores the result. Can an unprivileged caller of `collateral-remove` (mainnet/contracts/market/v0-4-market.clar:1107), by choosing whether the position has any enabled debt row (the has-debt branch), use that to turn an accounting residue into a permanently unclosable position, violating the invariant that every asset a position holds enters the health evaluation exactly once and producing permanent freezing of funds?

## Target
- File/function: `mainnet/contracts/market/v0-4-market.clar:262` -> `accrue-debt-asset`
- Entrypoint: `collateral-remove` (`mainnet/contracts/market/v0-4-market.clar:1107`), unprivileged and publicly callable
- Attacker controls: whether the position has any enabled debt row (the has-debt branch)
- Exploit idea: `accrue-debt-asset` calls `accrue-and-cache` with `unwrap-panic` inside a fold whose accumulator ignores the result. Reach it through `collateral-remove` and turn an accounting residue into a permanently unclosable position.
- Invariant to test: every asset a position holds enters the health evaluation exactly once
- Expected Immunefi impact: Critical - permanent freezing of funds
- Fast validation: Snapshot every state variable `accrue-debt-asset` touches, run `collateral-remove` with whether the position has any enabled debt row (the has-debt branch), recompute the invariant off-chain from the snapshot, and assert it matches the on-chain result.
