# Q2991: Address-book role bit isolation: book churn / stale auth residue / precise unregister

## Question
Can an unprivileged address-book owner controlling only its own registrations enter through `LoansAuth.registerAddress(Roles,address) and unregisterAddress(Roles,address)` with rapid register/unregister cycles for the same grantee across different roles while the same grantee address is intentionally present under multiple roles for legitimate reasons and make register/unregister churn leave a grantee usable in later flows after its intended role was supposedly removed, breaking the rule that unregistering one role should not leave or remove unrelated role authority unexpectedly and leading to Bypass of intended permissions and whitelisting rules?

## Target
- File/function: contracts/misc/LoansAuth.sol / registerAddress, unregisterAddress, isRegisteredForRole
- Entrypoint: LoansAuth.registerAddress(Roles,address) and unregisterAddress(Roles,address)
- Attacker controls: rapid register/unregister cycles for the same grantee across different roles
- Exploit idea: make register/unregister churn leave a grantee usable in later flows after its intended role was supposedly removed
- Invariant to test: unregistering one role should not leave or remove unrelated role authority unexpectedly
- Expected Immunefi impact: Bypass of intended permissions and whitelisting rules
- Fast validation: Forge test role-bit combinations and assert each downstream check accepts only the exact role and exact book intended.
