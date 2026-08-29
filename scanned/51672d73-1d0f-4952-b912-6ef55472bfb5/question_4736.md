# Q4736: send-tokens via transfer: convert a rounding direction into a repeatable extraction

## Question
Does `transfer` (mainnet/contracts/vault/v0-vault-stx.clar:752) let an unprivileged attacker who controls the timing relative to a pledge or a liquidation reach `send-tokens` (mainnet/contracts/market/v0-market-vault.clar:259) in a state where it convert a rounding direction into a repeatable extraction? Given that it pushes an asset to a caller-chosen recipient principal, the invariant that a vault's underlying plus outstanding debt covers all shares and all supplier claims breaks and the result is protocol insolvency.

## Target
- File/function: `mainnet/contracts/market/v0-market-vault.clar:259` -> `send-tokens`
- Entrypoint: `transfer` (`mainnet/contracts/vault/v0-vault-stx.clar:752`), unprivileged and publicly callable
- Attacker controls: the timing relative to a pledge or a liquidation
- Exploit idea: `send-tokens` pushes an asset to a caller-chosen recipient principal. Reach it through `transfer` and convert a rounding direction into a repeatable extraction.
- Invariant to test: a vault's underlying plus outstanding debt covers all shares and all supplier claims
- Expected Immunefi impact: Critical - protocol insolvency
- Fast validation: Write a Clarinet simnet test calling `transfer` twice with the timing relative to a pledge or a liquidation varied, and assert that the value `send-tokens` returns is identical in both runs; a divergence confirms the finding.
