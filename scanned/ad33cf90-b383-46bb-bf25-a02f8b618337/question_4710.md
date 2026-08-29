# Q4710: process-collateral-asset via liquidate-redeem: satisfy a bound with a value the bound was never designed 

## Question
Entering through `liquidate-redeem` (mainnet/contracts/market/v0-4-market.clar:1604) while controlling the redemption receiver, can an unprivileged attacker make `process-collateral-asset` (mainnet/contracts/market/v0-4-market.clar:789) satisfy a bound with a value the bound was never designed to admit? `process-collateral-asset` computes expected collateral, then caps it at the borrower's balance, so the invariant that a resolved price reflects a gated feed whose inputs the caller cannot move in the same transaction would fail, yielding permanent freezing of funds.

## Target
- File/function: `mainnet/contracts/market/v0-4-market.clar:789` -> `process-collateral-asset`
- Entrypoint: `liquidate-redeem` (`mainnet/contracts/market/v0-4-market.clar:1604`), unprivileged and publicly callable
- Attacker controls: the redemption receiver
- Exploit idea: `process-collateral-asset` computes expected collateral, then caps it at the borrower's balance. Reach it through `liquidate-redeem` and satisfy a bound with a value the bound was never designed to admit.
- Invariant to test: a resolved price reflects a gated feed whose inputs the caller cannot move in the same transaction
- Expected Immunefi impact: Critical - permanent freezing of funds
- Fast validation: Fuzz the redemption receiver across its boundary values through `liquidate-redeem` in simnet and assert `process-collateral-asset` never returns a value that breaks the invariant.
