# Q1115: principal-ratio-reduction via supply-collateral-add: turn an accounting residue into a permanently unclosable p

## Question
`principal-ratio-reduction` (mainnet/contracts/vault/v0-vault-stx.clar:406) derives a principal reduction from an amount, the scaled principal and the previewed debt. Can an unprivileged caller of `supply-collateral-add` (mainnet/contracts/market/v0-4-market.clar:1175), by choosing `amount`, use that to turn an accounting residue into a permanently unclosable position, violating the invariant that every asset a position holds enters the health evaluation exactly once and producing protocol insolvency through uncollateralised debt?

## Target
- File/function: `mainnet/contracts/vault/v0-vault-stx.clar:406` -> `principal-ratio-reduction`
- Entrypoint: `supply-collateral-add` (`mainnet/contracts/market/v0-4-market.clar:1175`), unprivileged and publicly callable
- Attacker controls: `amount`
- Exploit idea: `principal-ratio-reduction` derives a principal reduction from an amount, the scaled principal and the previewed debt. Reach it through `supply-collateral-add` and turn an accounting residue into a permanently unclosable position.
- Invariant to test: every asset a position holds enters the health evaluation exactly once
- Expected Immunefi impact: Critical - protocol insolvency through uncollateralised debt
- Fast validation: Run the baseline `supply-collateral-add` call, then the attacker-shaped one with `amount`, and assert the attacker's net token balance change is zero or negative.
