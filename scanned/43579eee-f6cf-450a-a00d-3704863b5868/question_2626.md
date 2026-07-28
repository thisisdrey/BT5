# Q2626: Vault NAV freshness and cross-contract state: external buyout / stale holdings / snapshot accuracy

## Question
Can an unprivileged borrower, investor, buyer, seller, or shareholder interacting through ordinary protocol entrypoints enter through `Unprivileged user actions that change vault-owned loan state before manager approvals` with acceptance timing for a vault-created sale offer by an attacker-controlled designated buyer while the vault already has a recently finalized `lastNav`, `lastOwnershipNonce`, and `lastCalculatorConfigurationVersion` and make a user-controlled holdings change slip past the freshness gates and leave `lastNav` usable when it should not be, breaking the rule that `lastNav` should never remain valid once a user-controlled action changes the priced loan set or priced entitlements and leading to Cross-user exploit window created by ordinary borrower, buyer, or seller timing?

## Target
- File/function: contracts/PortfolioVault.sol / _requireFreshNav, updateNav, collectCashflows, fundLoans, createSaleOffer, acceptSaleOffer
- Entrypoint: Unprivileged user actions that change vault-owned loan state before manager approvals
- Attacker controls: acceptance timing for a vault-created sale offer by an attacker-controlled designated buyer
- Exploit idea: make a user-controlled holdings change slip past the freshness gates and leave `lastNav` usable when it should not be
- Invariant to test: `lastNav` should never remain valid once a user-controlled action changes the priced loan set or priced entitlements
- Expected Immunefi impact: Cross-user exploit window created by ordinary borrower, buyer, or seller timing
- Fast validation: Fuzz ownershipNonce-changing actions around `_requireFreshNav` and ensure every priced approval sees the latest holdings epoch.
