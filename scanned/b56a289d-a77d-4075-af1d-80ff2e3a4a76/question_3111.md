# Q3111: Address-book role bit isolation: mutual whitelist / book bleed / precise unregister

## Question
Can an unprivileged address-book owner controlling only its own registrations enter through `LoansAuth.registerAddress(Roles,address) and unregisterAddress(Roles,address)` with seller/buyer or originator/counterparty relationships that depend on exact role bits while the same grantee address is intentionally present under multiple roles for legitimate reasons and make self-book updates affect canonical-book semantics or another address owner's semantics, breaking the rule that unregistering one role should not leave or remove unrelated role authority unexpectedly and leading to Bypass of intended permissions and whitelisting rules?

## Target
- File/function: contracts/misc/LoansAuth.sol / registerAddress, unregisterAddress, isRegisteredForRole
- Entrypoint: LoansAuth.registerAddress(Roles,address) and unregisterAddress(Roles,address)
- Attacker controls: seller/buyer or originator/counterparty relationships that depend on exact role bits
- Exploit idea: make self-book updates affect canonical-book semantics or another address owner's semantics
- Invariant to test: unregistering one role should not leave or remove unrelated role authority unexpectedly
- Expected Immunefi impact: Bypass of intended permissions and whitelisting rules
- Fast validation: Forge test role-bit combinations and assert each downstream check accepts only the exact role and exact book intended.
