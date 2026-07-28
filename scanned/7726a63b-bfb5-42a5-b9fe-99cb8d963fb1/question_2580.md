# Q2580: Vault NAV freshness and cross-contract state: borrower payment / stale holdings / no stale claims

## Question
Can an unprivileged borrower, investor, buyer, seller, or shareholder interacting through ordinary protocol entrypoints enter through `Unprivileged user actions that change vault-owned loan state before manager approvals` with a borrower payment into a vault-owned loan while deposit or redemption approvals are about to rely on `_requireFreshNav` and make a user-controlled holdings change slip past the freshness gates and leave `lastNav` usable when it should not be, breaking the rule that an unprivileged counterparty should never be able to force underpriced or overpriced vault claims through timing alone and leading to Unintended or unfair fund distribution through stale NAV approvals?

## Target
- File/function: contracts/PortfolioVault.sol / _requireFreshNav, updateNav, collectCashflows, fundLoans, createSaleOffer, acceptSaleOffer
- Entrypoint: Unprivileged user actions that change vault-owned loan state before manager approvals
- Attacker controls: a borrower payment into a vault-owned loan
- Exploit idea: make a user-controlled holdings change slip past the freshness gates and leave `lastNav` usable when it should not be
- Invariant to test: an unprivileged counterparty should never be able to force underpriced or overpriced vault claims through timing alone
- Expected Immunefi impact: Unintended or unfair fund distribution through stale NAV approvals
- Fast validation: Fuzz ownershipNonce-changing actions around `_requireFreshNav` and ensure every priced approval sees the latest holdings epoch.
