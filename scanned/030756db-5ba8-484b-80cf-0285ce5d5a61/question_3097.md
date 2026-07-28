# Q3097: Address-book role bit isolation: mutual whitelist / whitelist bypass / role isolation

## Question
Can an unprivileged address-book owner controlling only its own registrations enter through `LoansAuth.registerAddress(Roles,address) and unregisterAddress(Roles,address)` with seller/buyer or originator/counterparty relationships that depend on exact role bits while a later `createOffer` or `acceptOffer` flow will read the investor bit from both sides' books and make a counterparty pass a mutual-registration gate without the exact intended role bit being present, breaking the rule that each role bit should authorize only its own role and never another role and leading to Unintended or unfair fund distribution after a wrong-role counterparty enters the lifecycle?

## Target
- File/function: contracts/misc/LoansAuth.sol / registerAddress, unregisterAddress, isRegisteredForRole
- Entrypoint: LoansAuth.registerAddress(Roles,address) and unregisterAddress(Roles,address)
- Attacker controls: seller/buyer or originator/counterparty relationships that depend on exact role bits
- Exploit idea: make a counterparty pass a mutual-registration gate without the exact intended role bit being present
- Invariant to test: each role bit should authorize only its own role and never another role
- Expected Immunefi impact: Unintended or unfair fund distribution after a wrong-role counterparty enters the lifecycle
- Fast validation: Forge test role-bit combinations and assert each downstream check accepts only the exact role and exact book intended.
