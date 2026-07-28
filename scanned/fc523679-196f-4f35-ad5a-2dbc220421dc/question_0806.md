# Q0806: Loans payment clearing and borrower effects: charged-off pay / timing gap / entitlement isolation

## Question
Can an unprivileged borrower or payer acting only through normal `pay` calls enter through `Loans.pay(uint64,int128,uint48,bytes32)` with payments into a loan that is `ChargedOff` but still accepts borrower payments while an investor or vault cashflow collection could run soon after the borrower payment and create a borrower-controlled timing gap where real cash exists on-chain but pricing or withdrawal paths still see the wrong balances, breaking the rule that a borrower should not be able to distort another user's principal or interest entitlements just by timing ordinary payments and leading to User funds stuck or mispriced until a trusted role resolves the clearing state?

## Target
- File/function: contracts/Loans.sol / pay
- Entrypoint: Loans.pay(uint64,int128,uint48,bytes32)
- Attacker controls: payments into a loan that is `ChargedOff` but still accepts borrower payments
- Exploit idea: create a borrower-controlled timing gap where real cash exists on-chain but pricing or withdrawal paths still see the wrong balances
- Invariant to test: a borrower should not be able to distort another user's principal or interest entitlements just by timing ordinary payments
- Expected Immunefi impact: User funds stuck or mispriced until a trusted role resolves the clearing state
- Fast validation: Fuzz payment sizes and timestamps around due dates and assert `lastPaymentDate`, clearing, and cash never enter a contradictory state.
