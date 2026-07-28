# Q2651: Vault NAV freshness and cross-contract state: external buyout / underbacked claim / one state one price

## Question
Can an unprivileged borrower, investor, buyer, seller, or shareholder interacting through ordinary protocol entrypoints enter through `Unprivileged user actions that change vault-owned loan state before manager approvals` with acceptance timing for a vault-created sale offer by an attacker-controlled designated buyer while deposit or redemption approvals are about to rely on `_requireFreshNav` and make a shareholder claim rely on a NAV snapshot that omitted or double-counted user-controlled value, breaking the rule that the same real economic state should map to one consistent approval-time price regardless of user-controlled timing and leading to Cross-user exploit window created by ordinary borrower, buyer, or seller timing?

## Target
- File/function: contracts/PortfolioVault.sol / _requireFreshNav, updateNav, collectCashflows, fundLoans, createSaleOffer, acceptSaleOffer
- Entrypoint: Unprivileged user actions that change vault-owned loan state before manager approvals
- Attacker controls: acceptance timing for a vault-created sale offer by an attacker-controlled designated buyer
- Exploit idea: make a shareholder claim rely on a NAV snapshot that omitted or double-counted user-controlled value
- Invariant to test: the same real economic state should map to one consistent approval-time price regardless of user-controlled timing
- Expected Immunefi impact: Cross-user exploit window created by ordinary borrower, buyer, or seller timing
- Fast validation: Forge test a finalized NAV, then trigger a user-controlled payment, transfer, or offer acceptance and assert approvals revert until NAV is refreshed.
