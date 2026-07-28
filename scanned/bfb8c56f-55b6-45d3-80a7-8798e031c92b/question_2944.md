# Q2944: Address-book role bit isolation: role reuse / stale auth residue / exact whitelist

## Question
Can an unprivileged address-book owner controlling only its own registrations enter through `LoansAuth.registerAddress(Roles,address) and unregisterAddress(Roles,address)` with the same attacker-controlled grantee registered under several Roles values in the same book while the canonical `address(this)` book and a self-managed book both contain related addresses and make register/unregister churn leave a grantee usable in later flows after its intended role was supposedly removed, breaking the rule that loan creation and exchange counterpart checks should require the exact intended role bit in the exact intended book and leading to Unauthorized loan creation or exchange settlement path through role confusion?

## Target
- File/function: contracts/misc/LoansAuth.sol / registerAddress, unregisterAddress, isRegisteredForRole
- Entrypoint: LoansAuth.registerAddress(Roles,address) and unregisterAddress(Roles,address)
- Attacker controls: the same attacker-controlled grantee registered under several Roles values in the same book
- Exploit idea: make register/unregister churn leave a grantee usable in later flows after its intended role was supposedly removed
- Invariant to test: loan creation and exchange counterpart checks should require the exact intended role bit in the exact intended book
- Expected Immunefi impact: Unauthorized loan creation or exchange settlement path through role confusion
- Fast validation: Fuzz register/unregister churn across roles and ensure no stale authorization survives in create or exchange flows.
