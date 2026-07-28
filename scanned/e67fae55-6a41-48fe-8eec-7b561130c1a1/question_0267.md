# Q0267: Loans create authorization: approval race / role confusion / role isolation

## Question
Can an unprivileged caller with no guardian, admin, originator, or servicer powers enter through `Loans.create(address,address,address,address,int128,uint48)` with a normal transaction ordering race around canonical originator approval and self-registration while the canonical address book already contains at least one genuinely approved originator and reuse one role approval context to satisfy a different role check and initialize a loan with unauthorized parties, breaking the rule that borrower, investor, servicer, and originator authorization should come from the intended book and role only and leading to Bypass of intended permissions and role-based access control?

## Target
- File/function: contracts/Loans.sol / create
- Entrypoint: Loans.create(address,address,address,address,int128,uint48)
- Attacker controls: a normal transaction ordering race around canonical originator approval and self-registration
- Exploit idea: reuse one role approval context to satisfy a different role check and initialize a loan with unauthorized parties
- Invariant to test: borrower, investor, servicer, and originator authorization should come from the intended book and role only
- Expected Immunefi impact: Bypass of intended permissions and role-based access control
- Fast validation: Forge test a caller with only self-book registrations and assert `create` cannot succeed unless the canonical originator book explicitly approves the caller.
