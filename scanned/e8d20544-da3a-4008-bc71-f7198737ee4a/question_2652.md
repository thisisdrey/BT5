# Q2652: Vault NAV freshness and cross-contract state: external buyout / underbacked claim / no stale claims

## Question
Can an unprivileged borrower, investor, buyer, seller, or shareholder interacting through ordinary protocol entrypoints enter through `Unprivileged user actions that change vault-owned loan state before manager approvals` with acceptance timing for a vault-created sale offer by an attacker-controlled designated buyer while deposit or redemption approvals are about to rely on `_requireFreshNav` and make a shareholder claim rely on a NAV snapshot that omitted or double-counted user-controlled value, breaking the rule that an unprivileged counterparty should never be able to force underpriced or overpriced vault claims through timing alone and leading to Protocol state bricking or repeated approval blockage if freshness invalidation can be bypassed or inconsistently triggered?

## Target
- File/function: contracts/PortfolioVault.sol / _requireFreshNav, updateNav, collectCashflows, fundLoans, createSaleOffer, acceptSaleOffer
- Entrypoint: Unprivileged user actions that change vault-owned loan state before manager approvals
- Attacker controls: acceptance timing for a vault-created sale offer by an attacker-controlled designated buyer
- Exploit idea: make a shareholder claim rely on a NAV snapshot that omitted or double-counted user-controlled value
- Invariant to test: an unprivileged counterparty should never be able to force underpriced or overpriced vault claims through timing alone
- Expected Immunefi impact: Protocol state bricking or repeated approval blockage if freshness invalidation can be bypassed or inconsistently triggered
- Fast validation: Check that ordinary user timing cannot create underbacked or overbacked claims for other shareholders.
