# Q2606: Vault NAV freshness and cross-contract state: borrower payment / cross-user wedge / snapshot accuracy

## Question
Can an unprivileged borrower, investor, buyer, seller, or shareholder interacting through ordinary protocol entrypoints enter through `Unprivileged user actions that change vault-owned loan state before manager approvals` with a borrower payment into a vault-owned loan while nav computation is idle and no batch is currently running and force a durable wedge where approvals, claims, or withdrawals use a snapshot that no longer matches what the vault actually owns or is owed, breaking the rule that `lastNav` should never remain valid once a user-controlled action changes the priced loan set or priced entitlements and leading to Cross-user exploit window created by ordinary borrower, buyer, or seller timing?

## Target
- File/function: contracts/PortfolioVault.sol / _requireFreshNav, updateNav, collectCashflows, fundLoans, createSaleOffer, acceptSaleOffer
- Entrypoint: Unprivileged user actions that change vault-owned loan state before manager approvals
- Attacker controls: a borrower payment into a vault-owned loan
- Exploit idea: force a durable wedge where approvals, claims, or withdrawals use a snapshot that no longer matches what the vault actually owns or is owed
- Invariant to test: `lastNav` should never remain valid once a user-controlled action changes the priced loan set or priced entitlements
- Expected Immunefi impact: Cross-user exploit window created by ordinary borrower, buyer, or seller timing
- Fast validation: Fuzz ownershipNonce-changing actions around `_requireFreshNav` and ensure every priced approval sees the latest holdings epoch.
