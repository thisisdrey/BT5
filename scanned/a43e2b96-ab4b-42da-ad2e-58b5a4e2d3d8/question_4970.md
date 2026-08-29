# Q4970: merge-price via liquidate-redeem: make an aggregate and its per-item breakdown disagree

## Question
Entering through `liquidate-redeem` (mainnet/contracts/market/v0-4-market.clar:1604) while controlling the vault whose share price the redemption moves, can an unprivileged attacker make `merge-price` (mainnet/contracts/market/v0-4-market.clar:506) make an aggregate and its per-item breakdown disagree? `merge-price` attaches a price to an asset record by position in the fold, not by asset id, so the invariant that only the acting principal's own position is mutated would fail, yielding protocol insolvency.

## Target
- File/function: `mainnet/contracts/market/v0-4-market.clar:506` -> `merge-price`
- Entrypoint: `liquidate-redeem` (`mainnet/contracts/market/v0-4-market.clar:1604`), unprivileged and publicly callable
- Attacker controls: the vault whose share price the redemption moves
- Exploit idea: `merge-price` attaches a price to an asset record by position in the fold, not by asset id. Reach it through `liquidate-redeem` and make an aggregate and its per-item breakdown disagree.
- Invariant to test: only the acting principal's own position is mutated
- Expected Immunefi impact: Critical - protocol insolvency
- Fast validation: Write a Clarinet simnet test calling `liquidate-redeem` twice with the vault whose share price the redemption moves varied, and assert that the value `merge-price` returns is identical in both runs; a divergence confirms the finding.
