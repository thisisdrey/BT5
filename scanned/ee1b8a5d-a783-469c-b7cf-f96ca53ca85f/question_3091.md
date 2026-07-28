# Q3091: Address-book role bit isolation: mutual whitelist / bit confusion / precise unregister

## Question
Can an unprivileged address-book owner controlling only its own registrations enter through `LoansAuth.registerAddress(Roles,address) and unregisterAddress(Roles,address)` with seller/buyer or originator/counterparty relationships that depend on exact role bits while a later `createOffer` or `acceptOffer` flow will read the investor bit from both sides' books and make one role bit satisfy another role check or leave stale authorization after unregistering the wrong role, breaking the rule that unregistering one role should not leave or remove unrelated role authority unexpectedly and leading to Unintended or unfair fund distribution after a wrong-role counterparty enters the lifecycle?

## Target
- File/function: contracts/misc/LoansAuth.sol / registerAddress, unregisterAddress, isRegisteredForRole
- Entrypoint: LoansAuth.registerAddress(Roles,address) and unregisterAddress(Roles,address)
- Attacker controls: seller/buyer or originator/counterparty relationships that depend on exact role bits
- Exploit idea: make one role bit satisfy another role check or leave stale authorization after unregistering the wrong role
- Invariant to test: unregistering one role should not leave or remove unrelated role authority unexpectedly
- Expected Immunefi impact: Unintended or unfair fund distribution after a wrong-role counterparty enters the lifecycle
- Fast validation: Forge test role-bit combinations and assert each downstream check accepts only the exact role and exact book intended.
