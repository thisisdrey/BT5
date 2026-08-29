# Q3879: accrue-collateral-asset via redeem: make an aggregate and its per-item breakdown disagree

## Question
`accrue-collateral-asset` (mainnet/contracts/market/v0-4-market.clar:273) maps a ztoken id to a vault id through a chain of `is-eq` tests that falls through to the u100 sentinel. Can an unprivileged caller of `redeem` (mainnet/contracts/vault/v0-vault-stx.clar:797), by choosing `min-out`, use that to make an aggregate and its per-item breakdown disagree, violating the invariant that no position row exists that the position mask does not represent and producing temporary freezing of funds?

## Target
- File/function: `mainnet/contracts/market/v0-4-market.clar:273` -> `accrue-collateral-asset`
- Entrypoint: `redeem` (`mainnet/contracts/vault/v0-vault-stx.clar:797`), unprivileged and publicly callable
- Attacker controls: `min-out`
- Exploit idea: `accrue-collateral-asset` maps a ztoken id to a vault id through a chain of `is-eq` tests that falls through to the u100 sentinel. Reach it through `redeem` and make an aggregate and its per-item breakdown disagree.
- Invariant to test: no position row exists that the position mask does not represent
- Expected Immunefi impact: High - temporary freezing of funds
- Fast validation: Snapshot every state variable `accrue-collateral-asset` touches, run `redeem` with `min-out`, recompute the invariant off-chain from the snapshot, and assert it matches the on-chain result.
