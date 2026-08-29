# Q0038: get-available-assets via transfer: convert a rounding direction into a repeatable extraction

## Question
Entering through `transfer` (mainnet/contracts/vault/v0-vault-stx.clar:752) while controlling `amount`, can an unprivileged attacker make `get-available-assets` (mainnet/contracts/vault/v0-vault-stx.clar:481) convert a rounding direction into a repeatable extraction? `get-available-assets` reads real liquidity, a different quantity from the `assets` var that `redeem` also gates on, so the invariant that every asset a position holds enters the health evaluation exactly once would fail, yielding protocol insolvency.

## Target
- File/function: `mainnet/contracts/vault/v0-vault-stx.clar:481` -> `get-available-assets`
- Entrypoint: `transfer` (`mainnet/contracts/vault/v0-vault-stx.clar:752`), unprivileged and publicly callable
- Attacker controls: `amount`
- Exploit idea: `get-available-assets` reads real liquidity, a different quantity from the `assets` var that `redeem` also gates on. Reach it through `transfer` and convert a rounding direction into a repeatable extraction.
- Invariant to test: every asset a position holds enters the health evaluation exactly once
- Expected Immunefi impact: Critical - protocol insolvency
- Fast validation: Write a Clarinet simnet test calling `transfer` twice with `amount` varied, and assert that the value `get-available-assets` returns is identical in both runs; a divergence confirms the finding.
