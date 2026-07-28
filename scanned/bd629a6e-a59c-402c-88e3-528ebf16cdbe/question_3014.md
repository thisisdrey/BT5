# Q3014: Address-book role bit isolation: zero-like target / book bleed / book isolation

## Question
Can an unprivileged address-book owner controlling only its own registrations enter through `LoansAuth.registerAddress(Roles,address) and unregisterAddress(Roles,address)` with edge-case grantee addresses such as zero-like or contract addresses the attacker controls while a later `create` flow will read borrower, investor, or servicer bits from the chosen address book and make self-book updates affect canonical-book semantics or another address owner's semantics, breaking the rule that a self-managed address book should never satisfy canonical-book checks or another owner's checks and leading to Bypass of intended permissions and whitelisting rules?

## Target
- File/function: contracts/misc/LoansAuth.sol / registerAddress, unregisterAddress, isRegisteredForRole
- Entrypoint: LoansAuth.registerAddress(Roles,address) and unregisterAddress(Roles,address)
- Attacker controls: edge-case grantee addresses such as zero-like or contract addresses the attacker controls
- Exploit idea: make self-book updates affect canonical-book semantics or another address owner's semantics
- Invariant to test: a self-managed address book should never satisfy canonical-book checks or another owner's checks
- Expected Immunefi impact: Bypass of intended permissions and whitelisting rules
- Fast validation: Forge test role-bit combinations and assert each downstream check accepts only the exact role and exact book intended.
