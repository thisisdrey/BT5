# Q3117: Address-book role bit isolation: mutual whitelist / stale auth residue / role isolation

## Question
Can an unprivileged address-book owner controlling only its own registrations enter through `LoansAuth.registerAddress(Roles,address) and unregisterAddress(Roles,address)` with seller/buyer or originator/counterparty relationships that depend on exact role bits while the same grantee address is intentionally present under multiple roles for legitimate reasons and make register/unregister churn leave a grantee usable in later flows after its intended role was supposedly removed, breaking the rule that each role bit should authorize only its own role and never another role and leading to Bypass of intended permissions and whitelisting rules?

## Target
- File/function: contracts/misc/LoansAuth.sol / registerAddress, unregisterAddress, isRegisteredForRole
- Entrypoint: LoansAuth.registerAddress(Roles,address) and unregisterAddress(Roles,address)
- Attacker controls: seller/buyer or originator/counterparty relationships that depend on exact role bits
- Exploit idea: make register/unregister churn leave a grantee usable in later flows after its intended role was supposedly removed
- Invariant to test: each role bit should authorize only its own role and never another role
- Expected Immunefi impact: Bypass of intended permissions and whitelisting rules
- Fast validation: Forge test role-bit combinations and assert each downstream check accepts only the exact role and exact book intended.
