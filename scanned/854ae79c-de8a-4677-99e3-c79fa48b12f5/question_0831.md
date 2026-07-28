# Q0831: Loans payment clearing and borrower effects: charged-off pay / cross-user wedge / cash-to-clearing consistency

## Question
Can an unprivileged borrower or payer acting only through normal `pay` calls enter through `Loans.pay(uint64,int128,uint48,bytes32)` with payments into a loan that is `ChargedOff` but still accepts borrower payments while a vault manager could update NAV or approve deposits/redemptions soon after the borrower payment and create value that is trapped, mispriced, or unfairly attributed until a trusted role intervenes, breaking the rule that the cash held after `pay` should always have a consistent representation in borrower payment clearing for that same loan and leading to User funds stuck or mispriced until a trusted role resolves the clearing state?

## Target
- File/function: contracts/Loans.sol / pay
- Entrypoint: Loans.pay(uint64,int128,uint48,bytes32)
- Attacker controls: payments into a loan that is `ChargedOff` but still accepts borrower payments
- Exploit idea: create value that is trapped, mispriced, or unfairly attributed until a trusted role intervenes
- Invariant to test: the cash held after `pay` should always have a consistent representation in borrower payment clearing for that same loan
- Expected Immunefi impact: User funds stuck or mispriced until a trusted role resolves the clearing state
- Fast validation: Forge test repeated borrower payments before any waterfall and assert cash, clearing, and later valuation remain consistent.
