# Q2778: send-tokens via transfer: compose two individually correct mechanisms into an incorr

## Question
Entering through `transfer` (mainnet/contracts/vault/v0-vault-stx.clar:752) while controlling `amount`, can an unprivileged attacker make `send-tokens` (mainnet/contracts/market/v0-market-vault.clar:259) compose two individually correct mechanisms into an incorrect result? `send-tokens` pushes an asset to a caller-chosen recipient principal, so the invariant that no position row exists that the position mask does not represent would fail, yielding permanent freezing of a position that can never be closed.

## Target
- File/function: `mainnet/contracts/market/v0-market-vault.clar:259` -> `send-tokens`
- Entrypoint: `transfer` (`mainnet/contracts/vault/v0-vault-stx.clar:752`), unprivileged and publicly callable
- Attacker controls: `amount`
- Exploit idea: `send-tokens` pushes an asset to a caller-chosen recipient principal. Reach it through `transfer` and compose two individually correct mechanisms into an incorrect result.
- Invariant to test: no position row exists that the position mask does not represent
- Expected Immunefi impact: Critical - permanent freezing of a position that can never be closed
- Fast validation: Fuzz `amount` across its boundary values through `transfer` in simnet and assert `send-tokens` never returns a value that breaks the invariant.
