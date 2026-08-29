# Q0159: user-safe-mask via collateral-remove-redeem: compose two individually correct mechanisms into an incorr

## Question
`user-safe-mask` (mainnet/contracts/market/v0-4-market.clar:428) ANDs the user's collateral bits against the enabled bitmap but keeps ALL debt bits unfiltered. Can an unprivileged caller of `collateral-remove-redeem` (mainnet/contracts/market/v0-4-market.clar:1211), by choosing the zToken/underlying id mapping reached (the u100 sentinel branch), use that to compose two individually correct mechanisms into an incorrect result, violating the invariant that conversions never round in the user's favour in either direction and producing permanent freezing of a position that can never be closed?

## Target
- File/function: `mainnet/contracts/market/v0-4-market.clar:428` -> `user-safe-mask`
- Entrypoint: `collateral-remove-redeem` (`mainnet/contracts/market/v0-4-market.clar:1211`), unprivileged and publicly callable
- Attacker controls: the zToken/underlying id mapping reached (the u100 sentinel branch)
- Exploit idea: `user-safe-mask` ANDs the user's collateral bits against the enabled bitmap but keeps ALL debt bits unfiltered. Reach it through `collateral-remove-redeem` and compose two individually correct mechanisms into an incorrect result.
- Invariant to test: conversions never round in the user's favour in either direction
- Expected Immunefi impact: Critical - permanent freezing of a position that can never be closed
- Fast validation: Snapshot every state variable `user-safe-mask` touches, run `collateral-remove-redeem` with the zToken/underlying id mapping reached (the u100 sentinel branch), recompute the invariant off-chain from the snapshot, and assert it matches the on-chain result.
