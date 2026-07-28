# Q3009: Address-book role bit isolation: zero-like target / bit confusion / role isolation

## Question
Can an unprivileged address-book owner controlling only its own registrations enter through `LoansAuth.registerAddress(Roles,address) and unregisterAddress(Roles,address)` with edge-case grantee addresses such as zero-like or contract addresses the attacker controls while a later `create` flow will read borrower, investor, or servicer bits from the chosen address book and make one role bit satisfy another role check or leave stale authorization after unregistering the wrong role, breaking the rule that each role bit should authorize only its own role and never another role and leading to Unintended or unfair fund distribution after a wrong-role counterparty enters the lifecycle?

## Target
- File/function: contracts/misc/LoansAuth.sol / registerAddress, unregisterAddress, isRegisteredForRole
- Entrypoint: LoansAuth.registerAddress(Roles,address) and unregisterAddress(Roles,address)
- Attacker controls: edge-case grantee addresses such as zero-like or contract addresses the attacker controls
- Exploit idea: make one role bit satisfy another role check or leave stale authorization after unregistering the wrong role
- Invariant to test: each role bit should authorize only its own role and never another role
- Expected Immunefi impact: Unintended or unfair fund distribution after a wrong-role counterparty enters the lifecycle
- Fast validation: Model one self-managed book plus the canonical book and assert they can never substitute for each other.
