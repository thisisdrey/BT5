# Q2070: send-tokens via borrow: compose two individually correct mechanisms into an incorr

## Question
Entering through `borrow` (mainnet/contracts/market/v0-4-market.clar:1238) while controlling `receiver`, including a contract principal, can an unprivileged attacker make `send-tokens` (mainnet/contracts/market/v0-market-vault.clar:259) compose two individually correct mechanisms into an incorrect result? `send-tokens` pushes an asset to a caller-chosen recipient principal, so the invariant that a vault's underlying plus outstanding debt covers all shares and all supplier claims would fail, yielding temporary freezing of funds.

## Target
- File/function: `mainnet/contracts/market/v0-market-vault.clar:259` -> `send-tokens`
- Entrypoint: `borrow` (`mainnet/contracts/market/v0-4-market.clar:1238`), unprivileged and publicly callable
- Attacker controls: `receiver`, including a contract principal
- Exploit idea: `send-tokens` pushes an asset to a caller-chosen recipient principal. Reach it through `borrow` and compose two individually correct mechanisms into an incorrect result.
- Invariant to test: a vault's underlying plus outstanding debt covers all shares and all supplier claims
- Expected Immunefi impact: High - temporary freezing of funds
- Fast validation: Fuzz `receiver`, including a contract principal across its boundary values through `borrow` in simnet and assert `send-tokens` never returns a value that breaks the invariant.
