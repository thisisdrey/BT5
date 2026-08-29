# Q2466: next-index via redeem: compose two individually correct mechanisms into an incorr

## Question
Entering through `redeem` (mainnet/contracts/vault/v0-vault-stx.clar:797) while controlling `recipient`, can an unprivileged attacker make `next-index` (mainnet/contracts/vault/v0-vault-stx.clar:379) compose two individually correct mechanisms into an incorrect result? `next-index` returns the stale `index` unchanged when the accrue pause state is set, instead of reverting, so the invariant that no position row exists that the position mask does not represent would fail, yielding permanent freezing of a position that can never be closed.

## Target
- File/function: `mainnet/contracts/vault/v0-vault-stx.clar:379` -> `next-index`
- Entrypoint: `redeem` (`mainnet/contracts/vault/v0-vault-stx.clar:797`), unprivileged and publicly callable
- Attacker controls: `recipient`
- Exploit idea: `next-index` returns the stale `index` unchanged when the accrue pause state is set, instead of reverting. Reach it through `redeem` and compose two individually correct mechanisms into an incorrect result.
- Invariant to test: no position row exists that the position mask does not represent
- Expected Immunefi impact: Critical - permanent freezing of a position that can never be closed
- Fast validation: Fuzz `recipient` across its boundary values through `redeem` in simnet and assert `next-index` never returns a value that breaks the invariant.
