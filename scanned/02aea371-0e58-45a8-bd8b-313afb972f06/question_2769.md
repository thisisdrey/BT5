# Q2769: Vault NAV freshness and cross-contract state: cashflow withdrawal timing / stale holdings / freshness gate

## Question
Can an unprivileged borrower, investor, buyer, seller, or shareholder interacting through ordinary protocol entrypoints enter through `Unprivileged user actions that change vault-owned loan state before manager approvals` with ordinary investor-withdraw or cashflow-collection timing around a vault-held position while deposit or redemption approvals are about to rely on `_requireFreshNav` and make a user-controlled holdings change slip past the freshness gates and leave `lastNav` usable when it should not be, breaking the rule that every user-controlled change to vault holdings or user-withdrawable loan value should invalidate price-sensitive approvals before they execute and leading to Unintended or unfair fund distribution through stale NAV approvals?

## Target
- File/function: contracts/PortfolioVault.sol / _requireFreshNav, updateNav, collectCashflows, fundLoans, createSaleOffer, acceptSaleOffer
- Entrypoint: Unprivileged user actions that change vault-owned loan state before manager approvals
- Attacker controls: ordinary investor-withdraw or cashflow-collection timing around a vault-held position
- Exploit idea: make a user-controlled holdings change slip past the freshness gates and leave `lastNav` usable when it should not be
- Invariant to test: every user-controlled change to vault holdings or user-withdrawable loan value should invalidate price-sensitive approvals before they execute
- Expected Immunefi impact: Unintended or unfair fund distribution through stale NAV approvals
- Fast validation: Fuzz ownershipNonce-changing actions around `_requireFreshNav` and ensure every priced approval sees the latest holdings epoch.
