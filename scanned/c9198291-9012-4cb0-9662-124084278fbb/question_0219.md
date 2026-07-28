# Q0219: Loans create authorization: contract receiver / role confusion / role isolation

## Question
Can an unprivileged caller with no guardian, admin, originator, or servicer powers enter through `Loans.create(address,address,address,address,int128,uint48)` with an attacker-controlled contract as the proposed investor plus separate EOAs for the other roles while the attacker has only self-registered role bits in its own address book and no canonical approvals and reuse one role approval context to satisfy a different role check and initialize a loan with unauthorized parties, breaking the rule that borrower, investor, servicer, and originator authorization should come from the intended book and role only and leading to Bypass of intended permissions and role-based access control?

## Target
- File/function: contracts/Loans.sol / create
- Entrypoint: Loans.create(address,address,address,address,int128,uint48)
- Attacker controls: an attacker-controlled contract as the proposed investor plus separate EOAs for the other roles
- Exploit idea: reuse one role approval context to satisfy a different role check and initialize a loan with unauthorized parties
- Invariant to test: borrower, investor, servicer, and originator authorization should come from the intended book and role only
- Expected Immunefi impact: Bypass of intended permissions and role-based access control
- Fast validation: Build a minimal scenario where a toxic loan would later be fundable or priceable, and assert the creation step itself cannot be reached by an unprivileged caller.
