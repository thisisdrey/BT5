# Q1743: calculate-asset-notional-value via collateral-remove: turn an accounting residue into a permanently unclosable p

## Question
`calculate-asset-notional-value` (mainnet/contracts/market/v0-4-market.clar:544) normalizes collateral with round-down and debt with round-up, and calls `accrue-and-cache` with `unwrap-panic` inside the fold. Can an unprivileged caller of `collateral-remove` (mainnet/contracts/market/v0-4-market.clar:1107), by choosing whether the position has any enabled debt row (the has-debt branch), use that to turn an accounting residue into a permanently unclosable position, violating the invariant that a resolved price reflects a gated feed whose inputs the caller cannot move in the same transaction and producing permanent freezing of a position that can never be closed?

## Target
- File/function: `mainnet/contracts/market/v0-4-market.clar:544` -> `calculate-asset-notional-value`
- Entrypoint: `collateral-remove` (`mainnet/contracts/market/v0-4-market.clar:1107`), unprivileged and publicly callable
- Attacker controls: whether the position has any enabled debt row (the has-debt branch)
- Exploit idea: `calculate-asset-notional-value` normalizes collateral with round-down and debt with round-up, and calls `accrue-and-cache` with `unwrap-panic` inside the fold. Reach it through `collateral-remove` and turn an accounting residue into a permanently unclosable position.
- Invariant to test: a resolved price reflects a gated feed whose inputs the caller cannot move in the same transaction
- Expected Immunefi impact: Critical - permanent freezing of a position that can never be closed
- Fast validation: Snapshot every state variable `calculate-asset-notional-value` touches, run `collateral-remove` with whether the position has any enabled debt row (the has-debt branch), recompute the invariant off-chain from the snapshot, and assert it matches the on-chain result.
