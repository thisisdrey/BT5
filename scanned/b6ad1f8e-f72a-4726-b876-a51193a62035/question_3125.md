# Q3125: Address-book role bit isolation: mutual whitelist / book bleed / role isolation

## Question
Can an unprivileged address-book owner controlling only its own registrations enter through `LoansAuth.registerAddress(Roles,address) and unregisterAddress(Roles,address)` with seller/buyer or originator/counterparty relationships that depend on exact role bits while the canonical `address(this)` book and a self-managed book both contain related addresses and make self-book updates affect canonical-book semantics or another address owner's semantics, breaking the rule that each role bit should authorize only its own role and never another role and leading to Loans NFT or cashflow rights entering an unauthorized counterparty context?

## Target
- File/function: contracts/misc/LoansAuth.sol / registerAddress, unregisterAddress, isRegisteredForRole
- Entrypoint: LoansAuth.registerAddress(Roles,address) and unregisterAddress(Roles,address)
- Attacker controls: seller/buyer or originator/counterparty relationships that depend on exact role bits
- Exploit idea: make self-book updates affect canonical-book semantics or another address owner's semantics
- Invariant to test: each role bit should authorize only its own role and never another role
- Expected Immunefi impact: Loans NFT or cashflow rights entering an unauthorized counterparty context
- Fast validation: Check that mixed legitimate multi-role grantees do not accidentally satisfy unrelated authorization checks.
