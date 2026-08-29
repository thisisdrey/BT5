# Q5931: calc-liq-factor-bound via liquidate-multi: convert a rounding direction into a repeatable extraction

## Question
`calc-liq-factor-bound` (mainnet/contracts/market/v0-4-market.clar:718) scales the penalty between a min and a max, capped at the max. Can an unprivileged caller of `liquidate-multi` (mainnet/contracts/market/v0-4-market.clar:1593), by choosing the full batch list and its ordering, use that to convert a rounding direction into a repeatable extraction, violating the invariant that collateral seized equals debt repaid scaled by the penalty, and only above the liquidation LTV and producing permanent freezing of funds?

## Target
- File/function: `mainnet/contracts/market/v0-4-market.clar:718` -> `calc-liq-factor-bound`
- Entrypoint: `liquidate-multi` (`mainnet/contracts/market/v0-4-market.clar:1593`), unprivileged and publicly callable
- Attacker controls: the full batch list and its ordering
- Exploit idea: `calc-liq-factor-bound` scales the penalty between a min and a max, capped at the max. Reach it through `liquidate-multi` and convert a rounding direction into a repeatable extraction.
- Invariant to test: collateral seized equals debt repaid scaled by the penalty, and only above the liquidation LTV
- Expected Immunefi impact: Critical - permanent freezing of funds
- Fast validation: Snapshot every state variable `calc-liq-factor-bound` touches, run `liquidate-multi` with the full batch list and its ordering, recompute the invariant off-chain from the snapshot, and assert it matches the on-chain result.
