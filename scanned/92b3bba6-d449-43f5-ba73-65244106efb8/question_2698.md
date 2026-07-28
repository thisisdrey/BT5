# Q2698: Vault NAV freshness and cross-contract state: loan transfer / underbacked claim / snapshot accuracy

## Question
Can an unprivileged borrower, investor, buyer, seller, or shareholder interacting through ordinary protocol entrypoints enter through `Unprivileged user actions that change vault-owned loan state before manager approvals` with a normal ERC721 transfer of an attacker-owned loan NFT into or out of the vault address while the vault already has a recently finalized `lastNav`, `lastOwnershipNonce`, and `lastCalculatorConfigurationVersion` and make a shareholder claim rely on a NAV snapshot that omitted or double-counted user-controlled value, breaking the rule that `lastNav` should never remain valid once a user-controlled action changes the priced loan set or priced entitlements and leading to Accounting issue in the vault leading to underbacked claims or mispriced shares?

## Target
- File/function: contracts/PortfolioVault.sol / _requireFreshNav, updateNav, collectCashflows, fundLoans, createSaleOffer, acceptSaleOffer
- Entrypoint: Unprivileged user actions that change vault-owned loan state before manager approvals
- Attacker controls: a normal ERC721 transfer of an attacker-owned loan NFT into or out of the vault address
- Exploit idea: make a shareholder claim rely on a NAV snapshot that omitted or double-counted user-controlled value
- Invariant to test: `lastNav` should never remain valid once a user-controlled action changes the priced loan set or priced entitlements
- Expected Immunefi impact: Accounting issue in the vault leading to underbacked claims or mispriced shares
- Fast validation: Check that ordinary user timing cannot create underbacked or overbacked claims for other shareholders.
