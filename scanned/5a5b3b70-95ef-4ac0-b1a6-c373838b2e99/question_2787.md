# Q2787: Vault NAV freshness and cross-contract state: cashflow withdrawal timing / stale holdings / one state one price

## Question
Can an unprivileged borrower, investor, buyer, seller, or shareholder interacting through ordinary protocol entrypoints enter through `Unprivileged user actions that change vault-owned loan state before manager approvals` with ordinary investor-withdraw or cashflow-collection timing around a vault-held position while nav computation is idle and no batch is currently running and make a user-controlled holdings change slip past the freshness gates and leave `lastNav` usable when it should not be, breaking the rule that the same real economic state should map to one consistent approval-time price regardless of user-controlled timing and leading to Protocol state bricking or repeated approval blockage if freshness invalidation can be bypassed or inconsistently triggered?

## Target
- File/function: contracts/PortfolioVault.sol / _requireFreshNav, updateNav, collectCashflows, fundLoans, createSaleOffer, acceptSaleOffer
- Entrypoint: Unprivileged user actions that change vault-owned loan state before manager approvals
- Attacker controls: ordinary investor-withdraw or cashflow-collection timing around a vault-held position
- Exploit idea: make a user-controlled holdings change slip past the freshness gates and leave `lastNav` usable when it should not be
- Invariant to test: the same real economic state should map to one consistent approval-time price regardless of user-controlled timing
- Expected Immunefi impact: Protocol state bricking or repeated approval blockage if freshness invalidation can be bypassed or inconsistently triggered
- Fast validation: Model borrower payments and external offer settlements right before manager approvals and assert no stale snapshot remains usable.
