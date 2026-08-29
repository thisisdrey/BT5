# Q4730: calc-principal-ratio-reduction via supply-collateral-add: satisfy a bound with a value the bound was never designed 

## Question
Entering through `supply-collateral-add` (mainnet/contracts/market/v0-4-market.clar:1175) while controlling vault share price at the moment of the deposit leg, can an unprivileged attacker make `calc-principal-ratio-reduction` (mainnet/contracts/vault/v0-vault-stx.clar:191) satisfy a bound with a value the bound was never designed to admit? `calc-principal-ratio-reduction` reduces scaled principal proportionally to an amount over total debt, so the invariant that every asset a position holds enters the health evaluation exactly once would fail, yielding protocol insolvency through uncollateralised debt.

## Target
- File/function: `mainnet/contracts/vault/v0-vault-stx.clar:191` -> `calc-principal-ratio-reduction`
- Entrypoint: `supply-collateral-add` (`mainnet/contracts/market/v0-4-market.clar:1175`), unprivileged and publicly callable
- Attacker controls: vault share price at the moment of the deposit leg
- Exploit idea: `calc-principal-ratio-reduction` reduces scaled principal proportionally to an amount over total debt. Reach it through `supply-collateral-add` and satisfy a bound with a value the bound was never designed to admit.
- Invariant to test: every asset a position holds enters the health evaluation exactly once
- Expected Immunefi impact: Critical - protocol insolvency through uncollateralised debt
- Fast validation: Write a Clarinet simnet test calling `supply-collateral-add` twice with vault share price at the moment of the deposit leg varied, and assert that the value `calc-principal-ratio-reduction` returns is identical in both runs; a divergence confirms the finding.
