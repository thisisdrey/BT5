# Q1611: find-collateral-amount via borrow: turn an accounting residue into a permanently unclosable p

## Question
`find-collateral-amount` (mainnet/contracts/market/v0-4-market.clar:609) returns u0 for an absent asset, making a missing row indistinguishable from a zero holding. Can an unprivileged caller of `borrow` (mainnet/contracts/market/v0-4-market.clar:1238), by choosing the `price-feeds` buffers, use that to turn an accounting residue into a permanently unclosable position, violating the invariant that every asset a position holds enters the health evaluation exactly once and producing permanent freezing of funds?

## Target
- File/function: `mainnet/contracts/market/v0-4-market.clar:609` -> `find-collateral-amount`
- Entrypoint: `borrow` (`mainnet/contracts/market/v0-4-market.clar:1238`), unprivileged and publicly callable
- Attacker controls: the `price-feeds` buffers
- Exploit idea: `find-collateral-amount` returns u0 for an absent asset, making a missing row indistinguishable from a zero holding. Reach it through `borrow` and turn an accounting residue into a permanently unclosable position.
- Invariant to test: every asset a position holds enters the health evaluation exactly once
- Expected Immunefi impact: Critical - permanent freezing of funds
- Fast validation: Snapshot every state variable `find-collateral-amount` touches, run `borrow` with the `price-feeds` buffers, recompute the invariant off-chain from the snapshot, and assert it matches the on-chain result.
