# Q2873: Vault NAV freshness and cross-contract state: offer staging / underbacked claim / freshness gate

## Question
Can an unprivileged borrower, investor, buyer, seller, or shareholder interacting through ordinary protocol entrypoints enter through `Unprivileged user actions that change vault-owned loan state before manager approvals` with a seller-created offer that the vault may later accept after attacker-controlled timing changes while the affected loan is in or near the curated `_navLoanIds` set used for pricing and make a shareholder claim rely on a NAV snapshot that omitted or double-counted user-controlled value, breaking the rule that every user-controlled change to vault holdings or user-withdrawable loan value should invalidate price-sensitive approvals before they execute and leading to Accounting issue in the vault leading to underbacked claims or mispriced shares?

## Target
- File/function: contracts/PortfolioVault.sol / _requireFreshNav, updateNav, collectCashflows, fundLoans, createSaleOffer, acceptSaleOffer
- Entrypoint: Unprivileged user actions that change vault-owned loan state before manager approvals
- Attacker controls: a seller-created offer that the vault may later accept after attacker-controlled timing changes
- Exploit idea: make a shareholder claim rely on a NAV snapshot that omitted or double-counted user-controlled value
- Invariant to test: every user-controlled change to vault holdings or user-withdrawable loan value should invalidate price-sensitive approvals before they execute
- Expected Immunefi impact: Accounting issue in the vault leading to underbacked claims or mispriced shares
- Fast validation: Model borrower payments and external offer settlements right before manager approvals and assert no stale snapshot remains usable.
