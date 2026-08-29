# Q0279: receive-underlying via redeem: compose two individually correct mechanisms into an incorr

## Question
`receive-underlying` (mainnet/contracts/vault/v0-vault-stx.clar:291) pulls the underlying from a named account. Can an unprivileged caller of `redeem` (mainnet/contracts/vault/v0-vault-stx.clar:797), by choosing the vault's available liquidity relative to the redemption, use that to compose two individually correct mechanisms into an incorrect result, violating the invariant that conversions never round in the user's favour in either direction and producing temporary freezing of funds?

## Target
- File/function: `mainnet/contracts/vault/v0-vault-stx.clar:291` -> `receive-underlying`
- Entrypoint: `redeem` (`mainnet/contracts/vault/v0-vault-stx.clar:797`), unprivileged and publicly callable
- Attacker controls: the vault's available liquidity relative to the redemption
- Exploit idea: `receive-underlying` pulls the underlying from a named account. Reach it through `redeem` and compose two individually correct mechanisms into an incorrect result.
- Invariant to test: conversions never round in the user's favour in either direction
- Expected Immunefi impact: High - temporary freezing of funds
- Fast validation: Snapshot every state variable `receive-underlying` touches, run `redeem` with the vault's available liquidity relative to the redemption, recompute the invariant off-chain from the snapshot, and assert it matches the on-chain result.
