# Q0203: Loans create authorization: contract receiver / role confusion / role isolation

## Question
Can an unprivileged caller with no guardian, admin, originator, or servicer powers enter through `Loans.create(address,address,address,address,int128,uint48)` with an attacker-controlled contract as the proposed investor plus separate EOAs for the other roles while the canonical address book already contains at least one genuinely approved originator and reuse one role approval context to satisfy a different role check and initialize a loan with unauthorized parties, breaking the rule that borrower, investor, servicer, and originator authorization should come from the intended book and role only and leading to Accounting issue in Loans caused by unauthorized lifecycle initialization?

## Target
- File/function: contracts/Loans.sol / create
- Entrypoint: Loans.create(address,address,address,address,int128,uint48)
- Attacker controls: an attacker-controlled contract as the proposed investor plus separate EOAs for the other roles
- Exploit idea: reuse one role approval context to satisfy a different role check and initialize a loan with unauthorized parties
- Invariant to test: borrower, investor, servicer, and originator authorization should come from the intended book and role only
- Expected Immunefi impact: Accounting issue in Loans caused by unauthorized lifecycle initialization
- Fast validation: Fuzz `msg.sender`, the `originator` parameter, and address-book contents, then assert every successful create binds to a canonically approved originator only.
