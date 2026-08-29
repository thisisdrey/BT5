# Q0026: oracle-last-update via liquidate-multi: convert a rounding direction into a repeatable extraction

## Question
Entering through `liquidate-multi` (mainnet/contracts/market/v0-4-market.clar:1593) while controlling how many entries share one price snapshot (price-feeds is passed as none), can an unprivileged attacker make `oracle-last-update` (mainnet/contracts/market/v0-4-market.clar:939) convert a rounding direction into a repeatable extraction? `oracle-last-update` returns the stored monotonic timestamp for a `{type, ident}` key shared by every asset using that feed, so the invariant that a resolved price reflects a gated feed whose inputs the caller cannot move in the same transaction would fail, yielding protocol insolvency.

## Target
- File/function: `mainnet/contracts/market/v0-4-market.clar:939` -> `oracle-last-update`
- Entrypoint: `liquidate-multi` (`mainnet/contracts/market/v0-4-market.clar:1593`), unprivileged and publicly callable
- Attacker controls: how many entries share one price snapshot (price-feeds is passed as none)
- Exploit idea: `oracle-last-update` returns the stored monotonic timestamp for a `{type, ident}` key shared by every asset using that feed. Reach it through `liquidate-multi` and convert a rounding direction into a repeatable extraction.
- Invariant to test: a resolved price reflects a gated feed whose inputs the caller cannot move in the same transaction
- Expected Immunefi impact: Critical - protocol insolvency
- Fast validation: Write a Clarinet simnet test calling `liquidate-multi` twice with how many entries share one price snapshot (price-feeds is passed as none) varied, and assert that the value `oracle-last-update` returns is identical in both runs; a divergence confirms the finding.
