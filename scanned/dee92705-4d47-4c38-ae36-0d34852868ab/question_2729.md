# Q2729: Vault NAV freshness and cross-contract state: loan transfer / underbacked claim / freshness gate

## Question
Can an unprivileged borrower, investor, buyer, seller, or shareholder interacting through ordinary protocol entrypoints enter through `Unprivileged user actions that change vault-owned loan state before manager approvals` with a normal ERC721 transfer of an attacker-owned loan NFT into or out of the vault address while nav computation is idle and no batch is currently running and make a shareholder claim rely on a NAV snapshot that omitted or double-counted user-controlled value, breaking the rule that every user-controlled change to vault holdings or user-withdrawable loan value should invalidate price-sensitive approvals before they execute and leading to Cross-user exploit window created by ordinary borrower, buyer, or seller timing?

## Target
- File/function: contracts/PortfolioVault.sol / _requireFreshNav, updateNav, collectCashflows, fundLoans, createSaleOffer, acceptSaleOffer
- Entrypoint: Unprivileged user actions that change vault-owned loan state before manager approvals
- Attacker controls: a normal ERC721 transfer of an attacker-owned loan NFT into or out of the vault address
- Exploit idea: make a shareholder claim rely on a NAV snapshot that omitted or double-counted user-controlled value
- Invariant to test: every user-controlled change to vault holdings or user-withdrawable loan value should invalidate price-sensitive approvals before they execute
- Expected Immunefi impact: Cross-user exploit window created by ordinary borrower, buyer, or seller timing
- Fast validation: Fuzz ownershipNonce-changing actions around `_requireFreshNav` and ensure every priced approval sees the latest holdings epoch.
