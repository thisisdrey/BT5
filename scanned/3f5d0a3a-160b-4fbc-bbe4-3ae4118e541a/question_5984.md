# Q5984: zip via liquidate-redeem: compose two individually correct mechanisms into an incorr

## Question
Does `liquidate-redeem` (mainnet/contracts/market/v0-4-market.clar:1604) let an unprivileged attacker who controls the redemption receiver reach `zip` (mainnet/contracts/vault/v0-vault-stx.clar:226) in a state where it compose two individually correct mechanisms into an incorrect result? Given that it pairs the utilization and rate point lists element by element, the invariant that a resolved price reflects a gated feed whose inputs the caller cannot move in the same transaction breaks and the result is protocol insolvency.

## Target
- File/function: `mainnet/contracts/vault/v0-vault-stx.clar:226` -> `zip`
- Entrypoint: `liquidate-redeem` (`mainnet/contracts/market/v0-4-market.clar:1604`), unprivileged and publicly callable
- Attacker controls: the redemption receiver
- Exploit idea: `zip` pairs the utilization and rate point lists element by element. Reach it through `liquidate-redeem` and compose two individually correct mechanisms into an incorrect result.
- Invariant to test: a resolved price reflects a gated feed whose inputs the caller cannot move in the same transaction
- Expected Immunefi impact: Critical - protocol insolvency
- Fast validation: Write a Clarinet simnet test calling `liquidate-redeem` twice with the redemption receiver varied, and assert that the value `zip` returns is identical in both runs; a divergence confirms the finding.
