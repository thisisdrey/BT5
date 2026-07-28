# Q2661: Vault NAV freshness and cross-contract state: external buyout / stale ledger / freshness gate

## Question
Can an unprivileged borrower, investor, buyer, seller, or shareholder interacting through ordinary protocol entrypoints enter through `Unprivileged user actions that change vault-owned loan state before manager approvals` with acceptance timing for a vault-created sale offer by an attacker-controlled designated buyer while nav computation is idle and no batch is currently running and make a user-controlled entitlement or cashflow change create a price-sensitive stale-state window across approvals, breaking the rule that every user-controlled change to vault holdings or user-withdrawable loan value should invalidate price-sensitive approvals before they execute and leading to Unintended or unfair fund distribution through stale NAV approvals?

## Target
- File/function: contracts/PortfolioVault.sol / _requireFreshNav, updateNav, collectCashflows, fundLoans, createSaleOffer, acceptSaleOffer
- Entrypoint: Unprivileged user actions that change vault-owned loan state before manager approvals
- Attacker controls: acceptance timing for a vault-created sale offer by an attacker-controlled designated buyer
- Exploit idea: make a user-controlled entitlement or cashflow change create a price-sensitive stale-state window across approvals
- Invariant to test: every user-controlled change to vault holdings or user-withdrawable loan value should invalidate price-sensitive approvals before they execute
- Expected Immunefi impact: Unintended or unfair fund distribution through stale NAV approvals
- Fast validation: Forge test a finalized NAV, then trigger a user-controlled payment, transfer, or offer acceptance and assert approvals revert until NAV is refreshed.
