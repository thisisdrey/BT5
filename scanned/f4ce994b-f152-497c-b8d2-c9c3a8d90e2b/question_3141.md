# Q3141: Address-book role bit isolation: canonical adjacency / book bleed / role isolation

## Question
Can an unprivileged address-book owner controlling only its own registrations enter through `LoansAuth.registerAddress(Roles,address) and unregisterAddress(Roles,address)` with self-book actions performed near canonical-book admin changes but without any privileged access while a later `create` flow will read borrower, investor, or servicer bits from the chosen address book and make self-book updates affect canonical-book semantics or another address owner's semantics, breaking the rule that each role bit should authorize only its own role and never another role and leading to Unauthorized loan creation or exchange settlement path through role confusion?

## Target
- File/function: contracts/misc/LoansAuth.sol / registerAddress, unregisterAddress, isRegisteredForRole
- Entrypoint: LoansAuth.registerAddress(Roles,address) and unregisterAddress(Roles,address)
- Attacker controls: self-book actions performed near canonical-book admin changes but without any privileged access
- Exploit idea: make self-book updates affect canonical-book semantics or another address owner's semantics
- Invariant to test: each role bit should authorize only its own role and never another role
- Expected Immunefi impact: Unauthorized loan creation or exchange settlement path through role confusion
- Fast validation: Check that mixed legitimate multi-role grantees do not accidentally satisfy unrelated authorization checks.
