# Q2901: Address-book role bit isolation: role reuse / book bleed / role isolation

## Question
Can an unprivileged address-book owner controlling only its own registrations enter through `LoansAuth.registerAddress(Roles,address) and unregisterAddress(Roles,address)` with the same attacker-controlled grantee registered under several Roles values in the same book while a later `createOffer` or `acceptOffer` flow will read the investor bit from both sides' books and make self-book updates affect canonical-book semantics or another address owner's semantics, breaking the rule that each role bit should authorize only its own role and never another role and leading to Unintended or unfair fund distribution after a wrong-role counterparty enters the lifecycle?

## Target
- File/function: contracts/misc/LoansAuth.sol / registerAddress, unregisterAddress, isRegisteredForRole
- Entrypoint: LoansAuth.registerAddress(Roles,address) and unregisterAddress(Roles,address)
- Attacker controls: the same attacker-controlled grantee registered under several Roles values in the same book
- Exploit idea: make self-book updates affect canonical-book semantics or another address owner's semantics
- Invariant to test: each role bit should authorize only its own role and never another role
- Expected Immunefi impact: Unintended or unfair fund distribution after a wrong-role counterparty enters the lifecycle
- Fast validation: Forge test role-bit combinations and assert each downstream check accepts only the exact role and exact book intended.
