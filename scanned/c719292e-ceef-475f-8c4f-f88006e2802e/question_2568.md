# Q2568: Vault NAV freshness and cross-contract state: borrower payment / stale ledger / no stale claims

## Question
Can an unprivileged borrower, investor, buyer, seller, or shareholder interacting through ordinary protocol entrypoints enter through `Unprivileged user actions that change vault-owned loan state before manager approvals` with a borrower payment into a vault-owned loan while the vault already has a recently finalized `lastNav`, `lastOwnershipNonce`, and `lastCalculatorConfigurationVersion` and make a user-controlled entitlement or cashflow change create a price-sensitive stale-state window across approvals, breaking the rule that an unprivileged counterparty should never be able to force underpriced or overpriced vault claims through timing alone and leading to Unintended or unfair fund distribution through stale NAV approvals?

## Target
- File/function: contracts/PortfolioVault.sol / _requireFreshNav, updateNav, collectCashflows, fundLoans, createSaleOffer, acceptSaleOffer
- Entrypoint: Unprivileged user actions that change vault-owned loan state before manager approvals
- Attacker controls: a borrower payment into a vault-owned loan
- Exploit idea: make a user-controlled entitlement or cashflow change create a price-sensitive stale-state window across approvals
- Invariant to test: an unprivileged counterparty should never be able to force underpriced or overpriced vault claims through timing alone
- Expected Immunefi impact: Unintended or unfair fund distribution through stale NAV approvals
- Fast validation: Forge test a finalized NAV, then trigger a user-controlled payment, transfer, or offer acceptance and assert approvals revert until NAV is refreshed.
