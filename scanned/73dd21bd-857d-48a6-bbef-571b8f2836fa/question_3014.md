# Q3014: calc-liq-debt-repay-real via liquidate: turn an accounting residue into a permanently unclosable p

## Question
Entering through `liquidate` (mainnet/contracts/market/v0-4-market.clar:1382) while controlling which collateral and debt asset pair is targeted, can an unprivileged attacker make `calc-liq-debt-repay-real` (mainnet/contracts/market/v0-4-market.clar:733) turn an accounting residue into a permanently unclosable position? `calc-liq-debt-repay-real` re-derives debt from capped collateral by dividing by `(+ BPS liq-penalty)`, so the invariant that conversions never round in the user's favour in either direction would fail, yielding protocol insolvency.

## Target
- File/function: `mainnet/contracts/market/v0-4-market.clar:733` -> `calc-liq-debt-repay-real`
- Entrypoint: `liquidate` (`mainnet/contracts/market/v0-4-market.clar:1382`), unprivileged and publicly callable
- Attacker controls: which collateral and debt asset pair is targeted
- Exploit idea: `calc-liq-debt-repay-real` re-derives debt from capped collateral by dividing by `(+ BPS liq-penalty)`. Reach it through `liquidate` and turn an accounting residue into a permanently unclosable position.
- Invariant to test: conversions never round in the user's favour in either direction
- Expected Immunefi impact: Critical - protocol insolvency
- Fast validation: Write a Clarinet simnet test calling `liquidate` twice with which collateral and debt asset pair is targeted varied, and assert that the value `calc-liq-debt-repay-real` returns is identical in both runs; a divergence confirms the finding.
