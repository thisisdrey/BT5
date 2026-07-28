# Q2733: Vault NAV freshness and cross-contract state: loan transfer / cross-user wedge / freshness gate

## Question
Can an unprivileged borrower, investor, buyer, seller, or shareholder interacting through ordinary protocol entrypoints enter through `Unprivileged user actions that change vault-owned loan state before manager approvals` with a normal ERC721 transfer of an attacker-owned loan NFT into or out of the vault address while nav computation is idle and no batch is currently running and force a durable wedge where approvals, claims, or withdrawals use a snapshot that no longer matches what the vault actually owns or is owed, breaking the rule that every user-controlled change to vault holdings or user-withdrawable loan value should invalidate price-sensitive approvals before they execute and leading to Protocol state bricking or repeated approval blockage if freshness invalidation can be bypassed or inconsistently triggered?

## Target
- File/function: contracts/PortfolioVault.sol / _requireFreshNav, updateNav, collectCashflows, fundLoans, createSaleOffer, acceptSaleOffer
- Entrypoint: Unprivileged user actions that change vault-owned loan state before manager approvals
- Attacker controls: a normal ERC721 transfer of an attacker-owned loan NFT into or out of the vault address
- Exploit idea: force a durable wedge where approvals, claims, or withdrawals use a snapshot that no longer matches what the vault actually owns or is owed
- Invariant to test: every user-controlled change to vault holdings or user-withdrawable loan value should invalidate price-sensitive approvals before they execute
- Expected Immunefi impact: Protocol state bricking or repeated approval blockage if freshness invalidation can be bypassed or inconsistently triggered
- Fast validation: Model borrower payments and external offer settlements right before manager approvals and assert no stale snapshot remains usable.
