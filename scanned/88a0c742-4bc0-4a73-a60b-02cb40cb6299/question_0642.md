# Q0642: Loans payment clearing and borrower effects: exact amount / clearing mismatch / entitlement isolation

## Question
Can an unprivileged borrower or payer acting only through normal `pay` calls enter through `Loans.pay(uint64,int128,uint48,bytes32)` with exact payment sizes and timestamps across repeated borrower-controlled pay calls while the loan is `Active` with non-zero principal or interest receivables and make borrower payment clearing and actual cash diverge from what downstream entitlement or pricing logic assumes, breaking the rule that a borrower should not be able to distort another user's principal or interest entitlements just by timing ordinary payments and leading to Unintended or unfair fund distribution between current and future investors or shareholders?

## Target
- File/function: contracts/Loans.sol / pay
- Entrypoint: Loans.pay(uint64,int128,uint48,bytes32)
- Attacker controls: exact payment sizes and timestamps across repeated borrower-controlled pay calls
- Exploit idea: make borrower payment clearing and actual cash diverge from what downstream entitlement or pricing logic assumes
- Invariant to test: a borrower should not be able to distort another user's principal or interest entitlements just by timing ordinary payments
- Expected Immunefi impact: Unintended or unfair fund distribution between current and future investors or shareholders
- Fast validation: Check that repeated ordinary payments cannot create a durable wedge between on-chain cash and user-withdrawable or priceable balances.
