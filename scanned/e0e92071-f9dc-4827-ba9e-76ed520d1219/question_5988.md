# Q5988: vault-accrue via accrue: compose two individually correct mechanisms into an incorr

## Question
Does `accrue` (mainnet/contracts/vault/v0-vault-stx.clar:835) let an unprivileged attacker who controls whether an earlier call in the same block already advanced last-update reach `vault-accrue` (mainnet/contracts/market/v0-4-market.clar:189) in a state where it compose two individually correct mechanisms into an incorrect result? Given that it dispatches accrual to one of six vaults by asset id, the invariant that every asset a position holds enters the health evaluation exactly once breaks and the result is permanent freezing of funds.

## Target
- File/function: `mainnet/contracts/market/v0-4-market.clar:189` -> `vault-accrue`
- Entrypoint: `accrue` (`mainnet/contracts/vault/v0-vault-stx.clar:835`), unprivileged and publicly callable
- Attacker controls: whether an earlier call in the same block already advanced last-update
- Exploit idea: `vault-accrue` dispatches accrual to one of six vaults by asset id. Reach it through `accrue` and compose two individually correct mechanisms into an incorrect result.
- Invariant to test: every asset a position holds enters the health evaluation exactly once
- Expected Immunefi impact: Critical - permanent freezing of funds
- Fast validation: Fuzz whether an earlier call in the same block already advanced last-update across its boundary values through `accrue` in simnet and assert `vault-accrue` never returns a value that breaks the invariant.
