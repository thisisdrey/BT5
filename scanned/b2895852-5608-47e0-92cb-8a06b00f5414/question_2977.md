# Q2977: Address-book role bit isolation: book churn / bit confusion / role isolation

## Question
Can an unprivileged address-book owner controlling only its own registrations enter through `LoansAuth.registerAddress(Roles,address) and unregisterAddress(Roles,address)` with rapid register/unregister cycles for the same grantee across different roles while the same grantee address is intentionally present under multiple roles for legitimate reasons and make one role bit satisfy another role check or leave stale authorization after unregistering the wrong role, breaking the rule that each role bit should authorize only its own role and never another role and leading to Loans NFT or cashflow rights entering an unauthorized counterparty context?

## Target
- File/function: contracts/misc/LoansAuth.sol / registerAddress, unregisterAddress, isRegisteredForRole
- Entrypoint: LoansAuth.registerAddress(Roles,address) and unregisterAddress(Roles,address)
- Attacker controls: rapid register/unregister cycles for the same grantee across different roles
- Exploit idea: make one role bit satisfy another role check or leave stale authorization after unregistering the wrong role
- Invariant to test: each role bit should authorize only its own role and never another role
- Expected Immunefi impact: Loans NFT or cashflow rights entering an unauthorized counterparty context
- Fast validation: Fuzz register/unregister churn across roles and ensure no stale authorization survives in create or exchange flows.
