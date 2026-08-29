# Q0219: remove-user-scaled-debt via liquidate: compose two individually correct mechanisms into an incorr

## Question
`remove-user-scaled-debt` (mainnet/contracts/market/v0-market-vault.clar:244) deletes the row only on an exact zero, otherwise leaving a residue that keeps the mask bit set. Can an unprivileged caller of `liquidate` (mainnet/contracts/market/v0-4-market.clar:1382), by choosing `min-collateral-expected`, use that to compose two individually correct mechanisms into an incorrect result, violating the invariant that a value cached within a block still describes the state it was derived from and producing permanent freezing of funds?

## Target
- File/function: `mainnet/contracts/market/v0-market-vault.clar:244` -> `remove-user-scaled-debt`
- Entrypoint: `liquidate` (`mainnet/contracts/market/v0-4-market.clar:1382`), unprivileged and publicly callable
- Attacker controls: `min-collateral-expected`
- Exploit idea: `remove-user-scaled-debt` deletes the row only on an exact zero, otherwise leaving a residue that keeps the mask bit set. Reach it through `liquidate` and compose two individually correct mechanisms into an incorrect result.
- Invariant to test: a value cached within a block still describes the state it was derived from
- Expected Immunefi impact: Critical - permanent freezing of funds
- Fast validation: Snapshot every state variable `remove-user-scaled-debt` touches, run `liquidate` with `min-collateral-expected`, recompute the invariant off-chain from the snapshot, and assert it matches the on-chain result.
