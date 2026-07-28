# Q2784: Vault NAV freshness and cross-contract state: cashflow withdrawal timing / cross-user wedge / no stale claims

## Question
Can an unprivileged borrower, investor, buyer, seller, or shareholder interacting through ordinary protocol entrypoints enter through `Unprivileged user actions that change vault-owned loan state before manager approvals` with ordinary investor-withdraw or cashflow-collection timing around a vault-held position while deposit or redemption approvals are about to rely on `_requireFreshNav` and force a durable wedge where approvals, claims, or withdrawals use a snapshot that no longer matches what the vault actually owns or is owed, breaking the rule that an unprivileged counterparty should never be able to force underpriced or overpriced vault claims through timing alone and leading to Cross-user exploit window created by ordinary borrower, buyer, or seller timing?

## Target
- File/function: contracts/PortfolioVault.sol / _requireFreshNav, updateNav, collectCashflows, fundLoans, createSaleOffer, acceptSaleOffer
- Entrypoint: Unprivileged user actions that change vault-owned loan state before manager approvals
- Attacker controls: ordinary investor-withdraw or cashflow-collection timing around a vault-held position
- Exploit idea: force a durable wedge where approvals, claims, or withdrawals use a snapshot that no longer matches what the vault actually owns or is owed
- Invariant to test: an unprivileged counterparty should never be able to force underpriced or overpriced vault claims through timing alone
- Expected Immunefi impact: Cross-user exploit window created by ordinary borrower, buyer, or seller timing
- Fast validation: Forge test a finalized NAV, then trigger a user-controlled payment, transfer, or offer acceptance and assert approvals revert until NAV is refreshed.
