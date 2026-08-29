# Q1056: calc-liq-debt-repay-real via liquidate: make an aggregate and its per-item breakdown disagree

## Question
Does `liquidate` (mainnet/contracts/market/v0-4-market.clar:1382) let an unprivileged attacker who controls `borrower`, any third-party principal reach `calc-liq-debt-repay-real` (mainnet/contracts/market/v0-4-market.clar:733) in a state where it make an aggregate and its per-item breakdown disagree? Given that it re-derives debt from capped collateral by dividing by `(+ BPS liq-penalty)`, the invariant that a value cached within a block still describes the state it was derived from breaks and the result is permanent freezing of funds.

## Target
- File/function: `mainnet/contracts/market/v0-4-market.clar:733` -> `calc-liq-debt-repay-real`
- Entrypoint: `liquidate` (`mainnet/contracts/market/v0-4-market.clar:1382`), unprivileged and publicly callable
- Attacker controls: `borrower`, any third-party principal
- Exploit idea: `calc-liq-debt-repay-real` re-derives debt from capped collateral by dividing by `(+ BPS liq-penalty)`. Reach it through `liquidate` and make an aggregate and its per-item breakdown disagree.
- Invariant to test: a value cached within a block still describes the state it was derived from
- Expected Immunefi impact: Critical - permanent freezing of funds
- Fast validation: Fuzz `borrower`, any third-party principal across its boundary values through `liquidate` in simnet and assert `calc-liq-debt-repay-real` never returns a value that breaks the invariant.
