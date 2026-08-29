# Q1799: send-tokens via transfer: turn an accounting residue into a permanently unclosable p

## Question
`send-tokens` (mainnet/contracts/market/v0-market-vault.clar:259) pushes an asset to a caller-chosen recipient principal. Can an unprivileged caller of `transfer` (mainnet/contracts/vault/v0-vault-stx.clar:752), by choosing the timing relative to a pledge or a liquidation, use that to turn an accounting residue into a permanently unclosable position, violating the invariant that a resolved price reflects a gated feed whose inputs the caller cannot move in the same transaction and producing permanent freezing of unclaimed yield?

## Target
- File/function: `mainnet/contracts/market/v0-market-vault.clar:259` -> `send-tokens`
- Entrypoint: `transfer` (`mainnet/contracts/vault/v0-vault-stx.clar:752`), unprivileged and publicly callable
- Attacker controls: the timing relative to a pledge or a liquidation
- Exploit idea: `send-tokens` pushes an asset to a caller-chosen recipient principal. Reach it through `transfer` and turn an accounting residue into a permanently unclosable position.
- Invariant to test: a resolved price reflects a gated feed whose inputs the caller cannot move in the same transaction
- Expected Immunefi impact: High - permanent freezing of unclaimed yield
- Fast validation: Run the baseline `transfer` call, then the attacker-shaped one with the timing relative to a pledge or a liquidation, and assert the attacker's net token balance change is zero or negative.
