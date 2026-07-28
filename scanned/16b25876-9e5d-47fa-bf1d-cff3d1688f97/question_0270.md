# Q0270: Loans create authorization: approval race / toxic loan seed / caller binding

## Question
Can an unprivileged caller with no guardian, admin, originator, or servicer powers enter through `Loans.create(address,address,address,address,int128,uint48)` with a normal transaction ordering race around canonical originator approval and self-registration while the canonical address book already contains at least one genuinely approved originator and seed a loan that looks valid on-chain even though the attacker never passed the intended canonical-originator gate, breaking the rule that the `originator` parameter and the effective caller identity should never diverge for an unprivileged transaction and leading to Bypass of intended permissions and role-based access control?

## Target
- File/function: contracts/Loans.sol / create
- Entrypoint: Loans.create(address,address,address,address,int128,uint48)
- Attacker controls: a normal transaction ordering race around canonical originator approval and self-registration
- Exploit idea: seed a loan that looks valid on-chain even though the attacker never passed the intended canonical-originator gate
- Invariant to test: the `originator` parameter and the effective caller identity should never diverge for an unprivileged transaction
- Expected Immunefi impact: Bypass of intended permissions and role-based access control
- Fast validation: Forge test a caller with only self-book registrations and assert `create` cannot succeed unless the canonical originator book explicitly approves the caller.
