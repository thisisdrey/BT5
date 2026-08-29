# Q4256: unpack-u16 via borrow: convert a rounding direction into a repeatable extraction

## Question
Does `borrow` (mainnet/contracts/market/v0-4-market.clar:1238) let an unprivileged attacker who controls the order of accrual versus price resolution inside the let reach `unpack-u16` (mainnet/contracts/vault/v0-vault-stx.clar:259) in a state where it convert a rounding direction into a repeatable extraction? Given that it unpacks eight u16 curve fields from one packed word, the invariant that a vault's underlying plus outstanding debt covers all shares and all supplier claims breaks and the result is protocol insolvency.

## Target
- File/function: `mainnet/contracts/vault/v0-vault-stx.clar:259` -> `unpack-u16`
- Entrypoint: `borrow` (`mainnet/contracts/market/v0-4-market.clar:1238`), unprivileged and publicly callable
- Attacker controls: the order of accrual versus price resolution inside the let
- Exploit idea: `unpack-u16` unpacks eight u16 curve fields from one packed word. Reach it through `borrow` and convert a rounding direction into a repeatable extraction.
- Invariant to test: a vault's underlying plus outstanding debt covers all shares and all supplier claims
- Expected Immunefi impact: Critical - protocol insolvency
- Fast validation: Write a Clarinet simnet test calling `borrow` twice with the order of accrual versus price resolution inside the let varied, and assert that the value `unpack-u16` returns is identical in both runs; a divergence confirms the finding.
