# Q0279: Loans create authorization: approval race / impersonation / role isolation

## Question
Can an unprivileged caller with no guardian, admin, originator, or servicer powers enter through `Loans.create(address,address,address,address,int128,uint48)` with a normal transaction ordering race around canonical originator approval and self-registration while the attacker has only self-registered role bits in its own address book and no canonical approvals and make `msg.sender` and the effective originator identity diverge so the attacker originates on behalf of another address, breaking the rule that borrower, investor, servicer, and originator authorization should come from the intended book and role only and leading to Bypass of intended permissions and role-based access control?

## Target
- File/function: contracts/Loans.sol / create
- Entrypoint: Loans.create(address,address,address,address,int128,uint48)
- Attacker controls: a normal transaction ordering race around canonical originator approval and self-registration
- Exploit idea: make `msg.sender` and the effective originator identity diverge so the attacker originates on behalf of another address
- Invariant to test: borrower, investor, servicer, and originator authorization should come from the intended book and role only
- Expected Immunefi impact: Bypass of intended permissions and role-based access control
- Fast validation: Build a minimal scenario where a toxic loan would later be fundable or priceable, and assert the creation step itself cannot be reached by an unprivileged caller.
