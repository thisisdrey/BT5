# Q2909: Address-book role bit isolation: role reuse / stale auth residue / role isolation

## Question
Can an unprivileged address-book owner controlling only its own registrations enter through `LoansAuth.registerAddress(Roles,address) and unregisterAddress(Roles,address)` with the same attacker-controlled grantee registered under several Roles values in the same book while a later `createOffer` or `acceptOffer` flow will read the investor bit from both sides' books and make register/unregister churn leave a grantee usable in later flows after its intended role was supposedly removed, breaking the rule that each role bit should authorize only its own role and never another role and leading to Bypass of intended permissions and whitelisting rules?

## Target
- File/function: contracts/misc/LoansAuth.sol / registerAddress, unregisterAddress, isRegisteredForRole
- Entrypoint: LoansAuth.registerAddress(Roles,address) and unregisterAddress(Roles,address)
- Attacker controls: the same attacker-controlled grantee registered under several Roles values in the same book
- Exploit idea: make register/unregister churn leave a grantee usable in later flows after its intended role was supposedly removed
- Invariant to test: each role bit should authorize only its own role and never another role
- Expected Immunefi impact: Bypass of intended permissions and whitelisting rules
- Fast validation: Model one self-managed book plus the canonical book and assert they can never substitute for each other.
