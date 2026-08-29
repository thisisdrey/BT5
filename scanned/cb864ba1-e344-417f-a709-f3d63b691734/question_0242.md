# Q0242: write-feed via liquidate-redeem: convert a rounding direction into a repeatable extraction

## Question
Entering through `liquidate-redeem` (mainnet/contracts/market/v0-4-market.clar:1604) while controlling the redemption receiver, can an unprivileged attacker make `write-feed` (mainnet/contracts/market/v0-4-market.clar:129) convert a rounding direction into a repeatable extraction? `write-feed` applies one Pyth price-feed update and folds its status, so the invariant that a resolved price reflects a gated feed whose inputs the caller cannot move in the same transaction would fail, yielding protocol insolvency.

## Target
- File/function: `mainnet/contracts/market/v0-4-market.clar:129` -> `write-feed`
- Entrypoint: `liquidate-redeem` (`mainnet/contracts/market/v0-4-market.clar:1604`), unprivileged and publicly callable
- Attacker controls: the redemption receiver
- Exploit idea: `write-feed` applies one Pyth price-feed update and folds its status. Reach it through `liquidate-redeem` and convert a rounding direction into a repeatable extraction.
- Invariant to test: a resolved price reflects a gated feed whose inputs the caller cannot move in the same transaction
- Expected Immunefi impact: Critical - protocol insolvency
- Fast validation: Write a Clarinet simnet test calling `liquidate-redeem` twice with the redemption receiver varied, and assert that the value `write-feed` returns is identical in both runs; a divergence confirms the finding.
