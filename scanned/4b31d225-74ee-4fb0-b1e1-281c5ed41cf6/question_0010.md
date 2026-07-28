# Q0010: Loans create authorization: self-book spoof / role confusion / caller binding

## Question
Can an unprivileged caller with no guardian, admin, originator, or servicer powers enter through `Loans.create(address,address,address,address,int128,uint48)` with only a self-curated address book plus attacker-controlled borrower, investor, and servicer addresses while the canonical address book already contains at least one genuinely approved originator and reuse one role approval context to satisfy a different role check and initialize a loan with unauthorized parties, breaking the rule that the `originator` parameter and the effective caller identity should never diverge for an unprivileged transaction and leading to Accounting issue in Loans caused by unauthorized lifecycle initialization?

## Target
- File/function: contracts/Loans.sol / create
- Entrypoint: Loans.create(address,address,address,address,int128,uint48)
- Attacker controls: only a self-curated address book plus attacker-controlled borrower, investor, and servicer addresses
- Exploit idea: reuse one role approval context to satisfy a different role check and initialize a loan with unauthorized parties
- Invariant to test: the `originator` parameter and the effective caller identity should never diverge for an unprivileged transaction
- Expected Immunefi impact: Accounting issue in Loans caused by unauthorized lifecycle initialization
- Fast validation: Fuzz `msg.sender`, the `originator` parameter, and address-book contents, then assert every successful create binds to a canonically approved originator only.
