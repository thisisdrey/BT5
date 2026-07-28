# Q0766: Loans payment clearing and borrower effects: tiny-large mix / cross-user wedge / entitlement isolation

## Question
Can an unprivileged borrower or payer acting only through normal `pay` calls enter through `Loans.pay(uint64,int128,uint48,bytes32)` with alternating tiny and large payment amounts before any servicing batch runs while a vault manager could update NAV or approve deposits/redemptions soon after the borrower payment and create value that is trapped, mispriced, or unfairly attributed until a trusted role intervenes, breaking the rule that a borrower should not be able to distort another user's principal or interest entitlements just by timing ordinary payments and leading to Accounting issue in Loans ledger or Vault?

## Target
- File/function: contracts/Loans.sol / pay
- Entrypoint: Loans.pay(uint64,int128,uint48,bytes32)
- Attacker controls: alternating tiny and large payment amounts before any servicing batch runs
- Exploit idea: create value that is trapped, mispriced, or unfairly attributed until a trusted role intervenes
- Invariant to test: a borrower should not be able to distort another user's principal or interest entitlements just by timing ordinary payments
- Expected Immunefi impact: Accounting issue in Loans ledger or Vault
- Fast validation: Fuzz payment sizes and timestamps around due dates and assert `lastPaymentDate`, clearing, and cash never enter a contradictory state.
