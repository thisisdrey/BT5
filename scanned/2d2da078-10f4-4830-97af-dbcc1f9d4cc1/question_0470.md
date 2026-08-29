# Q0470: zip via collateral-remove-redeem: convert a rounding direction into a repeatable extraction

## Question
Entering through `collateral-remove-redeem` (mainnet/contracts/market/v0-4-market.clar:1211) while controlling remaining zToken collateral whose price moves with the redeem, can an unprivileged attacker make `zip` (mainnet/contracts/vault/v0-vault-stx.clar:226) convert a rounding direction into a repeatable extraction? `zip` pairs the utilization and rate point lists element by element, so the invariant that every asset a position holds enters the health evaluation exactly once would fail, yielding protocol insolvency.

## Target
- File/function: `mainnet/contracts/vault/v0-vault-stx.clar:226` -> `zip`
- Entrypoint: `collateral-remove-redeem` (`mainnet/contracts/market/v0-4-market.clar:1211`), unprivileged and publicly callable
- Attacker controls: remaining zToken collateral whose price moves with the redeem
- Exploit idea: `zip` pairs the utilization and rate point lists element by element. Reach it through `collateral-remove-redeem` and convert a rounding direction into a repeatable extraction.
- Invariant to test: every asset a position holds enters the health evaluation exactly once
- Expected Immunefi impact: Critical - protocol insolvency
- Fast validation: Write a Clarinet simnet test calling `collateral-remove-redeem` twice with remaining zToken collateral whose price moves with the redeem varied, and assert that the value `zip` returns is identical in both runs; a divergence confirms the finding.
