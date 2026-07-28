# Q3041: Address-book role bit isolation: zero-like target / bit confusion / role isolation

## Question
Can an unprivileged address-book owner controlling only its own registrations enter through `LoansAuth.registerAddress(Roles,address) and unregisterAddress(Roles,address)` with edge-case grantee addresses such as zero-like or contract addresses the attacker controls while the same grantee address is intentionally present under multiple roles for legitimate reasons and make one role bit satisfy another role check or leave stale authorization after unregistering the wrong role, breaking the rule that each role bit should authorize only its own role and never another role and leading to Bypass of intended permissions and whitelisting rules?

## Target
- File/function: contracts/misc/LoansAuth.sol / registerAddress, unregisterAddress, isRegisteredForRole
- Entrypoint: LoansAuth.registerAddress(Roles,address) and unregisterAddress(Roles,address)
- Attacker controls: edge-case grantee addresses such as zero-like or contract addresses the attacker controls
- Exploit idea: make one role bit satisfy another role check or leave stale authorization after unregistering the wrong role
- Invariant to test: each role bit should authorize only its own role and never another role
- Expected Immunefi impact: Bypass of intended permissions and whitelisting rules
- Fast validation: Forge test role-bit combinations and assert each downstream check accepts only the exact role and exact book intended.
