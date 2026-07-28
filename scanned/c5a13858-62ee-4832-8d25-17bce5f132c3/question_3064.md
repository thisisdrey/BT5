# Q3064: Address-book role bit isolation: zero-like target / book bleed / exact whitelist

## Question
Can an unprivileged address-book owner controlling only its own registrations enter through `LoansAuth.registerAddress(Roles,address) and unregisterAddress(Roles,address)` with edge-case grantee addresses such as zero-like or contract addresses the attacker controls while the canonical `address(this)` book and a self-managed book both contain related addresses and make self-book updates affect canonical-book semantics or another address owner's semantics, breaking the rule that loan creation and exchange counterpart checks should require the exact intended role bit in the exact intended book and leading to Unauthorized loan creation or exchange settlement path through role confusion?

## Target
- File/function: contracts/misc/LoansAuth.sol / registerAddress, unregisterAddress, isRegisteredForRole
- Entrypoint: LoansAuth.registerAddress(Roles,address) and unregisterAddress(Roles,address)
- Attacker controls: edge-case grantee addresses such as zero-like or contract addresses the attacker controls
- Exploit idea: make self-book updates affect canonical-book semantics or another address owner's semantics
- Invariant to test: loan creation and exchange counterpart checks should require the exact intended role bit in the exact intended book
- Expected Immunefi impact: Unauthorized loan creation or exchange settlement path through role confusion
- Fast validation: Fuzz register/unregister churn across roles and ensure no stale authorization survives in create or exchange flows.
