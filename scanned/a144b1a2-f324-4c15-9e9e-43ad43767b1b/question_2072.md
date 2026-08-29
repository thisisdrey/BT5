# Q2072: vault-accrue via accrue: make two code sites that must agree disagree by an attacke

## Question
Does `accrue` (mainnet/contracts/vault/v0-vault-stx.clar:835) let an unprivileged attacker who controls the block time at which accrual is first triggered in a block reach `vault-accrue` (mainnet/contracts/market/v0-4-market.clar:189) in a state where it make two code sites that must agree disagree by an attacker-chosen amount? Given that it dispatches accrual to one of six vaults by asset id, the invariant that every asset a position holds enters the health evaluation exactly once breaks and the result is protocol insolvency.

## Target
- File/function: `mainnet/contracts/market/v0-4-market.clar:189` -> `vault-accrue`
- Entrypoint: `accrue` (`mainnet/contracts/vault/v0-vault-stx.clar:835`), unprivileged and publicly callable
- Attacker controls: the block time at which accrual is first triggered in a block
- Exploit idea: `vault-accrue` dispatches accrual to one of six vaults by asset id. Reach it through `accrue` and make two code sites that must agree disagree by an attacker-chosen amount.
- Invariant to test: every asset a position holds enters the health evaluation exactly once
- Expected Immunefi impact: Critical - protocol insolvency
- Fast validation: Write a Clarinet simnet test calling `accrue` twice with the block time at which accrual is first triggered in a block varied, and assert that the value `vault-accrue` returns is identical in both runs; a divergence confirms the finding.
