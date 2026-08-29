# Q3830: calc-liq-debt-repay-real via liquidate-multi: turn an accounting residue into a permanently unclosable p

## Question
Entering through `liquidate-multi` (mainnet/contracts/market/v0-4-market.clar:1593) while controlling the full batch list and its ordering, can an unprivileged attacker make `calc-liq-debt-repay-real` (mainnet/contracts/market/v0-4-market.clar:733) turn an accounting residue into a permanently unclosable position? `calc-liq-debt-repay-real` re-derives debt from capped collateral by dividing by `(+ BPS liq-penalty)`, so the invariant that conversions never round in the user's favour in either direction would fail, yielding protocol insolvency through uncollateralised debt.

## Target
- File/function: `mainnet/contracts/market/v0-4-market.clar:733` -> `calc-liq-debt-repay-real`
- Entrypoint: `liquidate-multi` (`mainnet/contracts/market/v0-4-market.clar:1593`), unprivileged and publicly callable
- Attacker controls: the full batch list and its ordering
- Exploit idea: `calc-liq-debt-repay-real` re-derives debt from capped collateral by dividing by `(+ BPS liq-penalty)`. Reach it through `liquidate-multi` and turn an accounting residue into a permanently unclosable position.
- Invariant to test: conversions never round in the user's favour in either direction
- Expected Immunefi impact: Critical - protocol insolvency through uncollateralised debt
- Fast validation: Write a Clarinet simnet test calling `liquidate-multi` twice with the full batch list and its ordering varied, and assert that the value `calc-liq-debt-repay-real` returns is identical in both runs; a divergence confirms the finding.
