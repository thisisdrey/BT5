# Q5271: calc-final-liquidation-amounts via liquidate-redeem: reach a state the guard immediately upstream of it never c

## Question
`calc-final-liquidation-amounts` (mainnet/contracts/market/v0-4-market.clar:834) recomputes debt proportionally when collateral was capped, a SECOND re-derivation after `process-debt-asset` already capped once. Can an unprivileged caller of `liquidate-redeem` (mainnet/contracts/market/v0-4-market.clar:1604), by choosing the borrower targeted, use that to reach a state the guard immediately upstream of it never contemplated, violating the invariant that every asset a position holds enters the health evaluation exactly once and producing permanent freezing of a position that can never be closed?

## Target
- File/function: `mainnet/contracts/market/v0-4-market.clar:834` -> `calc-final-liquidation-amounts`
- Entrypoint: `liquidate-redeem` (`mainnet/contracts/market/v0-4-market.clar:1604`), unprivileged and publicly callable
- Attacker controls: the borrower targeted
- Exploit idea: `calc-final-liquidation-amounts` recomputes debt proportionally when collateral was capped, a SECOND re-derivation after `process-debt-asset` already capped once. Reach it through `liquidate-redeem` and reach a state the guard immediately upstream of it never contemplated.
- Invariant to test: every asset a position holds enters the health evaluation exactly once
- Expected Immunefi impact: Critical - permanent freezing of a position that can never be closed
- Fast validation: Snapshot every state variable `calc-final-liquidation-amounts` touches, run `liquidate-redeem` with the borrower targeted, recompute the invariant off-chain from the snapshot, and assert it matches the on-chain result.
