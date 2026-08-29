# Q0558: calc-index-next via redeem: convert a rounding direction into a repeatable extraction

## Question
Entering through `redeem` (mainnet/contracts/vault/v0-vault-stx.clar:797) while controlling `recipient`, can an unprivileged attacker make `calc-index-next` (mainnet/contracts/vault/v0-vault-stx.clar:183) convert a rounding direction into a repeatable extraction? `calc-index-next` applies a multiplier to the current index, so the invariant that every asset a position holds enters the health evaluation exactly once would fail, yielding temporary freezing of funds.

## Target
- File/function: `mainnet/contracts/vault/v0-vault-stx.clar:183` -> `calc-index-next`
- Entrypoint: `redeem` (`mainnet/contracts/vault/v0-vault-stx.clar:797`), unprivileged and publicly callable
- Attacker controls: `recipient`
- Exploit idea: `calc-index-next` applies a multiplier to the current index. Reach it through `redeem` and convert a rounding direction into a repeatable extraction.
- Invariant to test: every asset a position holds enters the health evaluation exactly once
- Expected Immunefi impact: High - temporary freezing of funds
- Fast validation: Fuzz `recipient` across its boundary values through `redeem` in simnet and assert `calc-index-next` never returns a value that breaks the invariant.
