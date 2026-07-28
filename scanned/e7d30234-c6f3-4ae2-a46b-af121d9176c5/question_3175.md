# Q3175: Address-book role bit isolation: canonical adjacency / book bleed / precise unregister

## Question
Can an unprivileged address-book owner controlling only its own registrations enter through `LoansAuth.registerAddress(Roles,address) and unregisterAddress(Roles,address)` with self-book actions performed near canonical-book admin changes but without any privileged access while the same grantee address is intentionally present under multiple roles for legitimate reasons and make self-book updates affect canonical-book semantics or another address owner's semantics, breaking the rule that unregistering one role should not leave or remove unrelated role authority unexpectedly and leading to Unauthorized loan creation or exchange settlement path through role confusion?

## Target
- File/function: contracts/misc/LoansAuth.sol / registerAddress, unregisterAddress, isRegisteredForRole
- Entrypoint: LoansAuth.registerAddress(Roles,address) and unregisterAddress(Roles,address)
- Attacker controls: self-book actions performed near canonical-book admin changes but without any privileged access
- Exploit idea: make self-book updates affect canonical-book semantics or another address owner's semantics
- Invariant to test: unregistering one role should not leave or remove unrelated role authority unexpectedly
- Expected Immunefi impact: Unauthorized loan creation or exchange settlement path through role confusion
- Fast validation: Check that mixed legitimate multi-role grantees do not accidentally satisfy unrelated authorization checks.
