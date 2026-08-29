# Q5951: calc-liq-debt-repay-real via liquidate: convert a rounding direction into a repeatable extraction

## Question
`calc-liq-debt-repay-real` (mainnet/contracts/market/v0-4-market.clar:733) re-derives debt from capped collateral by dividing by `(+ BPS liq-penalty)`. Can an unprivileged caller of `liquidate` (mainnet/contracts/market/v0-4-market.clar:1382), by choosing which collateral and debt asset pair is targeted, use that to convert a rounding direction into a repeatable extraction, violating the invariant that only the acting principal's own position is mutated and producing protocol insolvency?

## Target
- File/function: `mainnet/contracts/market/v0-4-market.clar:733` -> `calc-liq-debt-repay-real`
- Entrypoint: `liquidate` (`mainnet/contracts/market/v0-4-market.clar:1382`), unprivileged and publicly callable
- Attacker controls: which collateral and debt asset pair is targeted
- Exploit idea: `calc-liq-debt-repay-real` re-derives debt from capped collateral by dividing by `(+ BPS liq-penalty)`. Reach it through `liquidate` and convert a rounding direction into a repeatable extraction.
- Invariant to test: only the acting principal's own position is mutated
- Expected Immunefi impact: Critical - protocol insolvency
- Fast validation: Run the baseline `liquidate` call, then the attacker-shaped one with which collateral and debt asset pair is targeted, and assert the attacker's net token balance change is zero or negative.
