# Q2850: Vault NAV freshness and cross-contract state: offer staging / stale holdings / snapshot accuracy

## Question
Can an unprivileged borrower, investor, buyer, seller, or shareholder interacting through ordinary protocol entrypoints enter through `Unprivileged user actions that change vault-owned loan state before manager approvals` with a seller-created offer that the vault may later accept after attacker-controlled timing changes while nav computation is idle and no batch is currently running and make a user-controlled holdings change slip past the freshness gates and leave `lastNav` usable when it should not be, breaking the rule that `lastNav` should never remain valid once a user-controlled action changes the priced loan set or priced entitlements and leading to Protocol state bricking or repeated approval blockage if freshness invalidation can be bypassed or inconsistently triggered?

## Target
- File/function: contracts/PortfolioVault.sol / _requireFreshNav, updateNav, collectCashflows, fundLoans, createSaleOffer, acceptSaleOffer
- Entrypoint: Unprivileged user actions that change vault-owned loan state before manager approvals
- Attacker controls: a seller-created offer that the vault may later accept after attacker-controlled timing changes
- Exploit idea: make a user-controlled holdings change slip past the freshness gates and leave `lastNav` usable when it should not be
- Invariant to test: `lastNav` should never remain valid once a user-controlled action changes the priced loan set or priced entitlements
- Expected Immunefi impact: Protocol state bricking or repeated approval blockage if freshness invalidation can be bypassed or inconsistently triggered
- Fast validation: Model borrower payments and external offer settlements right before manager approvals and assert no stale snapshot remains usable.
