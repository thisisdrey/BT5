# Q5955: get-notional-evaluation via liquidate-redeem: convert a rounding direction into a repeatable extraction

## Question
`get-notional-evaluation` (mainnet/contracts/market/v0-4-market.clar:514) folds over the ENABLED asset list, so a position row whose asset is absent from that list contributes nothing to either total. Can an unprivileged caller of `liquidate-redeem` (mainnet/contracts/market/v0-4-market.clar:1604), by choosing the seized zToken amount that is immediately redeemed, use that to convert a rounding direction into a repeatable extraction, violating the invariant that collateral seized equals debt repaid scaled by the penalty, and only above the liquidation LTV and producing permanent freezing of a position that can never be closed?

## Target
- File/function: `mainnet/contracts/market/v0-4-market.clar:514` -> `get-notional-evaluation`
- Entrypoint: `liquidate-redeem` (`mainnet/contracts/market/v0-4-market.clar:1604`), unprivileged and publicly callable
- Attacker controls: the seized zToken amount that is immediately redeemed
- Exploit idea: `get-notional-evaluation` folds over the ENABLED asset list, so a position row whose asset is absent from that list contributes nothing to either total. Reach it through `liquidate-redeem` and convert a rounding direction into a repeatable extraction.
- Invariant to test: collateral seized equals debt repaid scaled by the penalty, and only above the liquidation LTV
- Expected Immunefi impact: Critical - permanent freezing of a position that can never be closed
- Fast validation: Snapshot every state variable `get-notional-evaluation` touches, run `liquidate-redeem` with the seized zToken amount that is immediately redeemed, recompute the invariant off-chain from the snapshot, and assert it matches the on-chain result.
