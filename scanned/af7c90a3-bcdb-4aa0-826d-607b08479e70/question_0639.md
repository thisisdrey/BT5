# Q0639: calc-principal-ratio-reduction via deposit: compose two individually correct mechanisms into an incorr

## Question
`calc-principal-ratio-reduction` (mainnet/contracts/vault/v0-vault-stx.clar:191) reduces scaled principal proportionally to an amount over total debt. Can an unprivileged caller of `deposit` (mainnet/contracts/vault/v0-vault-stx.clar:763), by choosing the vault's supply and asset state at the moment of the call, use that to compose two individually correct mechanisms into an incorrect result, violating the invariant that conversions never round in the user's favour in either direction and producing temporary freezing of funds?

## Target
- File/function: `mainnet/contracts/vault/v0-vault-stx.clar:191` -> `calc-principal-ratio-reduction`
- Entrypoint: `deposit` (`mainnet/contracts/vault/v0-vault-stx.clar:763`), unprivileged and publicly callable
- Attacker controls: the vault's supply and asset state at the moment of the call
- Exploit idea: `calc-principal-ratio-reduction` reduces scaled principal proportionally to an amount over total debt. Reach it through `deposit` and compose two individually correct mechanisms into an incorrect result.
- Invariant to test: conversions never round in the user's favour in either direction
- Expected Immunefi impact: High - temporary freezing of funds
- Fast validation: Snapshot every state variable `calc-principal-ratio-reduction` touches, run `deposit` with the vault's supply and asset state at the moment of the call, recompute the invariant off-chain from the snapshot, and assert it matches the on-chain result.
