# Q5979: mask-pos via collateral-add: convert a rounding direction into a repeatable extraction

## Question
`mask-pos` (mainnet/contracts/market/v0-market-vault.clar:91) maps an asset id to a bit position, offsetting debt bits by DEBT-OFFSET. Can an unprivileged caller of `collateral-add` (mainnet/contracts/market/v0-4-market.clar:1020), by choosing the three `price-feeds` buffers and their order, use that to convert a rounding direction into a repeatable extraction, violating the invariant that collateral seized equals debt repaid scaled by the penalty, and only above the liquidation LTV and producing permanent freezing of funds?

## Target
- File/function: `mainnet/contracts/market/v0-market-vault.clar:91` -> `mask-pos`
- Entrypoint: `collateral-add` (`mainnet/contracts/market/v0-4-market.clar:1020`), unprivileged and publicly callable
- Attacker controls: the three `price-feeds` buffers and their order
- Exploit idea: `mask-pos` maps an asset id to a bit position, offsetting debt bits by DEBT-OFFSET. Reach it through `collateral-add` and convert a rounding direction into a repeatable extraction.
- Invariant to test: collateral seized equals debt repaid scaled by the penalty, and only above the liquidation LTV
- Expected Immunefi impact: Critical - permanent freezing of funds
- Fast validation: Snapshot every state variable `mask-pos` touches, run `collateral-add` with the three `price-feeds` buffers and their order, recompute the invariant off-chain from the snapshot, and assert it matches the on-chain result.
