# Q2761: Vault NAV freshness and cross-contract state: cashflow withdrawal timing / underbacked claim / freshness gate

## Question
Can an unprivileged borrower, investor, buyer, seller, or shareholder interacting through ordinary protocol entrypoints enter through `Unprivileged user actions that change vault-owned loan state before manager approvals` with ordinary investor-withdraw or cashflow-collection timing around a vault-held position while the vault already has a recently finalized `lastNav`, `lastOwnershipNonce`, and `lastCalculatorConfigurationVersion` and make a shareholder claim rely on a NAV snapshot that omitted or double-counted user-controlled value, breaking the rule that every user-controlled change to vault holdings or user-withdrawable loan value should invalidate price-sensitive approvals before they execute and leading to Accounting issue in the vault leading to underbacked claims or mispriced shares?

## Target
- File/function: contracts/PortfolioVault.sol / _requireFreshNav, updateNav, collectCashflows, fundLoans, createSaleOffer, acceptSaleOffer
- Entrypoint: Unprivileged user actions that change vault-owned loan state before manager approvals
- Attacker controls: ordinary investor-withdraw or cashflow-collection timing around a vault-held position
- Exploit idea: make a shareholder claim rely on a NAV snapshot that omitted or double-counted user-controlled value
- Invariant to test: every user-controlled change to vault holdings or user-withdrawable loan value should invalidate price-sensitive approvals before they execute
- Expected Immunefi impact: Accounting issue in the vault leading to underbacked claims or mispriced shares
- Fast validation: Check that ordinary user timing cannot create underbacked or overbacked claims for other shareholders.
