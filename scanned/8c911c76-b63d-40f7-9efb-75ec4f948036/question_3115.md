# Q3115: Address-book role bit isolation: mutual whitelist / whitelist bypass / precise unregister

## Question
Can an unprivileged address-book owner controlling only its own registrations enter through `LoansAuth.registerAddress(Roles,address) and unregisterAddress(Roles,address)` with seller/buyer or originator/counterparty relationships that depend on exact role bits while the same grantee address is intentionally present under multiple roles for legitimate reasons and make a counterparty pass a mutual-registration gate without the exact intended role bit being present, breaking the rule that unregistering one role should not leave or remove unrelated role authority unexpectedly and leading to Unauthorized loan creation or exchange settlement path through role confusion?

## Target
- File/function: contracts/misc/LoansAuth.sol / registerAddress, unregisterAddress, isRegisteredForRole
- Entrypoint: LoansAuth.registerAddress(Roles,address) and unregisterAddress(Roles,address)
- Attacker controls: seller/buyer or originator/counterparty relationships that depend on exact role bits
- Exploit idea: make a counterparty pass a mutual-registration gate without the exact intended role bit being present
- Invariant to test: unregistering one role should not leave or remove unrelated role authority unexpectedly
- Expected Immunefi impact: Unauthorized loan creation or exchange settlement path through role confusion
- Fast validation: Check that mixed legitimate multi-role grantees do not accidentally satisfy unrelated authorization checks.
