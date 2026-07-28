# Q0722: Loans payment clearing and borrower effects: tiny-large mix / clearing mismatch / entitlement isolation

## Question
Can an unprivileged borrower or payer acting only through normal `pay` calls enter through `Loans.pay(uint64,int128,uint48,bytes32)` with alternating tiny and large payment amounts before any servicing batch runs while no waterfall has yet moved the fresh payment out of `ACC_BORROWER_PAYMENT_CLEARING` and make borrower payment clearing and actual cash diverge from what downstream entitlement or pricing logic assumes, breaking the rule that a borrower should not be able to distort another user's principal or interest entitlements just by timing ordinary payments and leading to Cross-user exploit window against NAV-sensitive vault operations?

## Target
- File/function: contracts/Loans.sol / pay
- Entrypoint: Loans.pay(uint64,int128,uint48,bytes32)
- Attacker controls: alternating tiny and large payment amounts before any servicing batch runs
- Exploit idea: make borrower payment clearing and actual cash diverge from what downstream entitlement or pricing logic assumes
- Invariant to test: a borrower should not be able to distort another user's principal or interest entitlements just by timing ordinary payments
- Expected Immunefi impact: Cross-user exploit window against NAV-sensitive vault operations
- Fast validation: Check that repeated ordinary payments cannot create a durable wedge between on-chain cash and user-withdrawable or priceable balances.
