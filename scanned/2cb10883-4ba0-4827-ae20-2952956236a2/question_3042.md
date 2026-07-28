# Q3042: Address-book role bit isolation: zero-like target / bit confusion / book isolation

## Question
Can an unprivileged address-book owner controlling only its own registrations enter through `LoansAuth.registerAddress(Roles,address) and unregisterAddress(Roles,address)` with edge-case grantee addresses such as zero-like or contract addresses the attacker controls while the same grantee address is intentionally present under multiple roles for legitimate reasons and make one role bit satisfy another role check or leave stale authorization after unregistering the wrong role, breaking the rule that a self-managed address book should never satisfy canonical-book checks or another owner's checks and leading to Unauthorized loan creation or exchange settlement path through role confusion?

## Target
- File/function: contracts/misc/LoansAuth.sol / registerAddress, unregisterAddress, isRegisteredForRole
- Entrypoint: LoansAuth.registerAddress(Roles,address) and unregisterAddress(Roles,address)
- Attacker controls: edge-case grantee addresses such as zero-like or contract addresses the attacker controls
- Exploit idea: make one role bit satisfy another role check or leave stale authorization after unregistering the wrong role
- Invariant to test: a self-managed address book should never satisfy canonical-book checks or another owner's checks
- Expected Immunefi impact: Unauthorized loan creation or exchange settlement path through role confusion
- Fast validation: Check that mixed legitimate multi-role grantees do not accidentally satisfy unrelated authorization checks.
