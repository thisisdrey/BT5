# Q3078: Address-book role bit isolation: mutual whitelist / book bleed / book isolation

## Question
Can an unprivileged address-book owner controlling only its own registrations enter through `LoansAuth.registerAddress(Roles,address) and unregisterAddress(Roles,address)` with seller/buyer or originator/counterparty relationships that depend on exact role bits while a later `create` flow will read borrower, investor, or servicer bits from the chosen address book and make self-book updates affect canonical-book semantics or another address owner's semantics, breaking the rule that a self-managed address book should never satisfy canonical-book checks or another owner's checks and leading to Unauthorized loan creation or exchange settlement path through role confusion?

## Target
- File/function: contracts/misc/LoansAuth.sol / registerAddress, unregisterAddress, isRegisteredForRole
- Entrypoint: LoansAuth.registerAddress(Roles,address) and unregisterAddress(Roles,address)
- Attacker controls: seller/buyer or originator/counterparty relationships that depend on exact role bits
- Exploit idea: make self-book updates affect canonical-book semantics or another address owner's semantics
- Invariant to test: a self-managed address book should never satisfy canonical-book checks or another owner's checks
- Expected Immunefi impact: Unauthorized loan creation or exchange settlement path through role confusion
- Fast validation: Check that mixed legitimate multi-role grantees do not accidentally satisfy unrelated authorization checks.
