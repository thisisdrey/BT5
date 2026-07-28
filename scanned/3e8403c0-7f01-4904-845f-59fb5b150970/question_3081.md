# Q3081: Address-book role bit isolation: mutual whitelist / whitelist bypass / role isolation

## Question
Can an unprivileged address-book owner controlling only its own registrations enter through `LoansAuth.registerAddress(Roles,address) and unregisterAddress(Roles,address)` with seller/buyer or originator/counterparty relationships that depend on exact role bits while a later `create` flow will read borrower, investor, or servicer bits from the chosen address book and make a counterparty pass a mutual-registration gate without the exact intended role bit being present, breaking the rule that each role bit should authorize only its own role and never another role and leading to Unauthorized loan creation or exchange settlement path through role confusion?

## Target
- File/function: contracts/misc/LoansAuth.sol / registerAddress, unregisterAddress, isRegisteredForRole
- Entrypoint: LoansAuth.registerAddress(Roles,address) and unregisterAddress(Roles,address)
- Attacker controls: seller/buyer or originator/counterparty relationships that depend on exact role bits
- Exploit idea: make a counterparty pass a mutual-registration gate without the exact intended role bit being present
- Invariant to test: each role bit should authorize only its own role and never another role
- Expected Immunefi impact: Unauthorized loan creation or exchange settlement path through role confusion
- Fast validation: Check that mixed legitimate multi-role grantees do not accidentally satisfy unrelated authorization checks.
