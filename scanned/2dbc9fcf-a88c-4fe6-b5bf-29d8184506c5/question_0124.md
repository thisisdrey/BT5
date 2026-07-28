# Q0124: Loans create authorization: originator mismatch / role confusion / downstream safety

## Question
Can an unprivileged caller with no guardian, admin, originator, or servicer powers enter through `Loans.create(address,address,address,address,int128,uint48)` with a fake `originator` parameter that names another address while the attacker remains `msg.sender` while a victim investor or vault could later interact with the created loan through funding, withdrawal, or pricing flows and reuse one role approval context to satisfy a different role check and initialize a loan with unauthorized parties, breaking the rule that an unprivileged caller should never be able to create a loan whose later funding, withdrawal, or pricing can touch another user without a real originator approval and leading to Unintended or unfair fund distribution once the unauthorized loan enters normal funding and withdrawal flows?

## Target
- File/function: contracts/Loans.sol / create
- Entrypoint: Loans.create(address,address,address,address,int128,uint48)
- Attacker controls: a fake `originator` parameter that names another address while the attacker remains `msg.sender`
- Exploit idea: reuse one role approval context to satisfy a different role check and initialize a loan with unauthorized parties
- Invariant to test: an unprivileged caller should never be able to create a loan whose later funding, withdrawal, or pricing can touch another user without a real originator approval
- Expected Immunefi impact: Unintended or unfair fund distribution once the unauthorized loan enters normal funding and withdrawal flows
- Fast validation: Fuzz `msg.sender`, the `originator` parameter, and address-book contents, then assert every successful create binds to a canonically approved originator only.
