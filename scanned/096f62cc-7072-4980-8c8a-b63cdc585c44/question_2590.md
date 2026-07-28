# Q2590: Vault NAV freshness and cross-contract state: borrower payment / cross-user wedge / snapshot accuracy

## Question
Can an unprivileged borrower, investor, buyer, seller, or shareholder interacting through ordinary protocol entrypoints enter through `Unprivileged user actions that change vault-owned loan state before manager approvals` with a borrower payment into a vault-owned loan while deposit or redemption approvals are about to rely on `_requireFreshNav` and force a durable wedge where approvals, claims, or withdrawals use a snapshot that no longer matches what the vault actually owns or is owed, breaking the rule that `lastNav` should never remain valid once a user-controlled action changes the priced loan set or priced entitlements and leading to Accounting issue in the vault leading to underbacked claims or mispriced shares?

## Target
- File/function: contracts/PortfolioVault.sol / _requireFreshNav, updateNav, collectCashflows, fundLoans, createSaleOffer, acceptSaleOffer
- Entrypoint: Unprivileged user actions that change vault-owned loan state before manager approvals
- Attacker controls: a borrower payment into a vault-owned loan
- Exploit idea: force a durable wedge where approvals, claims, or withdrawals use a snapshot that no longer matches what the vault actually owns or is owed
- Invariant to test: `lastNav` should never remain valid once a user-controlled action changes the priced loan set or priced entitlements
- Expected Immunefi impact: Accounting issue in the vault leading to underbacked claims or mispriced shares
- Fast validation: Model borrower payments and external offer settlements right before manager approvals and assert no stale snapshot remains usable.
