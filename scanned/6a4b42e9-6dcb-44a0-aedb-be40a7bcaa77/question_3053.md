# Q3053: Address-book role bit isolation: zero-like target / stale auth residue / role isolation

## Question
Can an unprivileged address-book owner controlling only its own registrations enter through `LoansAuth.registerAddress(Roles,address) and unregisterAddress(Roles,address)` with edge-case grantee addresses such as zero-like or contract addresses the attacker controls while the same grantee address is intentionally present under multiple roles for legitimate reasons and make register/unregister churn leave a grantee usable in later flows after its intended role was supposedly removed, breaking the rule that each role bit should authorize only its own role and never another role and leading to Loans NFT or cashflow rights entering an unauthorized counterparty context?

## Target
- File/function: contracts/misc/LoansAuth.sol / registerAddress, unregisterAddress, isRegisteredForRole
- Entrypoint: LoansAuth.registerAddress(Roles,address) and unregisterAddress(Roles,address)
- Attacker controls: edge-case grantee addresses such as zero-like or contract addresses the attacker controls
- Exploit idea: make register/unregister churn leave a grantee usable in later flows after its intended role was supposedly removed
- Invariant to test: each role bit should authorize only its own role and never another role
- Expected Immunefi impact: Loans NFT or cashflow rights entering an unauthorized counterparty context
- Fast validation: Fuzz register/unregister churn across roles and ensure no stale authorization survives in create or exchange flows.
