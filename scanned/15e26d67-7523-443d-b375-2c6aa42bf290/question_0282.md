# Q0282: Loans create authorization: approval race / role confusion / caller binding

## Question
Can an unprivileged caller with no guardian, admin, originator, or servicer powers enter through `Loans.create(address,address,address,address,int128,uint48)` with a normal transaction ordering race around canonical originator approval and self-registration while the attacker has only self-registered role bits in its own address book and no canonical approvals and reuse one role approval context to satisfy a different role check and initialize a loan with unauthorized parties, breaking the rule that the `originator` parameter and the effective caller identity should never diverge for an unprivileged transaction and leading to Bypass of intended permissions and role-based access control?

## Target
- File/function: contracts/Loans.sol / create
- Entrypoint: Loans.create(address,address,address,address,int128,uint48)
- Attacker controls: a normal transaction ordering race around canonical originator approval and self-registration
- Exploit idea: reuse one role approval context to satisfy a different role check and initialize a loan with unauthorized parties
- Invariant to test: the `originator` parameter and the effective caller identity should never diverge for an unprivileged transaction
- Expected Immunefi impact: Bypass of intended permissions and role-based access control
- Fast validation: Build a minimal scenario where a toxic loan would later be fundable or priceable, and assert the creation step itself cannot be reached by an unprivileged caller.
