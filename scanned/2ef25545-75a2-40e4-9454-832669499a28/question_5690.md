# Q5690: zip via repay: make an aggregate and its per-item breakdown disagree

## Question
Entering through `repay` (mainnet/contracts/market/v0-4-market.clar:1316) while controlling the `ft` trait principal, can an unprivileged attacker make `zip` (mainnet/contracts/vault/v0-vault-stx.clar:226) make an aggregate and its per-item breakdown disagree? `zip` pairs the utilization and rate point lists element by element, so the invariant that only the acting principal's own position is mutated would fail, yielding protocol insolvency.

## Target
- File/function: `mainnet/contracts/vault/v0-vault-stx.clar:226` -> `zip`
- Entrypoint: `repay` (`mainnet/contracts/market/v0-4-market.clar:1316`), unprivileged and publicly callable
- Attacker controls: the `ft` trait principal
- Exploit idea: `zip` pairs the utilization and rate point lists element by element. Reach it through `repay` and make an aggregate and its per-item breakdown disagree.
- Invariant to test: only the acting principal's own position is mutated
- Expected Immunefi impact: Critical - protocol insolvency
- Fast validation: Write a Clarinet simnet test calling `repay` twice with the `ft` trait principal varied, and assert that the value `zip` returns is identical in both runs; a divergence confirms the finding.
