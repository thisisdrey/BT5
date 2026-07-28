# Q2838: Vault NAV freshness and cross-contract state: offer staging / stale ledger / snapshot accuracy

## Question
Can an unprivileged borrower, investor, buyer, seller, or shareholder interacting through ordinary protocol entrypoints enter through `Unprivileged user actions that change vault-owned loan state before manager approvals` with a seller-created offer that the vault may later accept after attacker-controlled timing changes while deposit or redemption approvals are about to rely on `_requireFreshNav` and make a user-controlled entitlement or cashflow change create a price-sensitive stale-state window across approvals, breaking the rule that `lastNav` should never remain valid once a user-controlled action changes the priced loan set or priced entitlements and leading to Protocol state bricking or repeated approval blockage if freshness invalidation can be bypassed or inconsistently triggered?

## Target
- File/function: contracts/PortfolioVault.sol / _requireFreshNav, updateNav, collectCashflows, fundLoans, createSaleOffer, acceptSaleOffer
- Entrypoint: Unprivileged user actions that change vault-owned loan state before manager approvals
- Attacker controls: a seller-created offer that the vault may later accept after attacker-controlled timing changes
- Exploit idea: make a user-controlled entitlement or cashflow change create a price-sensitive stale-state window across approvals
- Invariant to test: `lastNav` should never remain valid once a user-controlled action changes the priced loan set or priced entitlements
- Expected Immunefi impact: Protocol state bricking or repeated approval blockage if freshness invalidation can be bypassed or inconsistently triggered
- Fast validation: Check that ordinary user timing cannot create underbacked or overbacked claims for other shareholders.
