# Q0811: Loans payment clearing and borrower effects: charged-off pay / date inconsistency / cash-to-clearing consistency

## Question
Can an unprivileged borrower or payer acting only through normal `pay` calls enter through `Loans.pay(uint64,int128,uint48,bytes32)` with payments into a loan that is `ChargedOff` but still accepts borrower payments while an investor or vault cashflow collection could run soon after the borrower payment and make repeated `pay` calls leave `lastPaymentDate` and economic balances in an order that later logic misinterprets, breaking the rule that the cash held after `pay` should always have a consistent representation in borrower payment clearing for that same loan and leading to Accounting issue in Loans ledger or Vault?

## Target
- File/function: contracts/Loans.sol / pay
- Entrypoint: Loans.pay(uint64,int128,uint48,bytes32)
- Attacker controls: payments into a loan that is `ChargedOff` but still accepts borrower payments
- Exploit idea: make repeated `pay` calls leave `lastPaymentDate` and economic balances in an order that later logic misinterprets
- Invariant to test: the cash held after `pay` should always have a consistent representation in borrower payment clearing for that same loan
- Expected Immunefi impact: Accounting issue in Loans ledger or Vault
- Fast validation: Forge test repeated borrower payments before any waterfall and assert cash, clearing, and later valuation remain consistent.
