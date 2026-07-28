# Q2583: Vault NAV freshness and cross-contract state: borrower payment / stale ledger / one state one price

## Question
Can an unprivileged borrower, investor, buyer, seller, or shareholder interacting through ordinary protocol entrypoints enter through `Unprivileged user actions that change vault-owned loan state before manager approvals` with a borrower payment into a vault-owned loan while deposit or redemption approvals are about to rely on `_requireFreshNav` and make a user-controlled entitlement or cashflow change create a price-sensitive stale-state window across approvals, breaking the rule that the same real economic state should map to one consistent approval-time price regardless of user-controlled timing and leading to Unintended or unfair fund distribution through stale NAV approvals?

## Target
- File/function: contracts/PortfolioVault.sol / _requireFreshNav, updateNav, collectCashflows, fundLoans, createSaleOffer, acceptSaleOffer
- Entrypoint: Unprivileged user actions that change vault-owned loan state before manager approvals
- Attacker controls: a borrower payment into a vault-owned loan
- Exploit idea: make a user-controlled entitlement or cashflow change create a price-sensitive stale-state window across approvals
- Invariant to test: the same real economic state should map to one consistent approval-time price regardless of user-controlled timing
- Expected Immunefi impact: Unintended or unfair fund distribution through stale NAV approvals
- Fast validation: Fuzz ownershipNonce-changing actions around `_requireFreshNav` and ensure every priced approval sees the latest holdings epoch.
