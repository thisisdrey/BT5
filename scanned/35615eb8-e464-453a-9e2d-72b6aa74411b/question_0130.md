# Q0130: Loans create authorization: role overlap / unauthorized create / caller binding

## Question
Can an unprivileged caller with no guardian, admin, originator, or servicer powers enter through `Loans.create(address,address,address,address,int128,uint48)` with the same attacker-controlled address reused across multiple loan roles while the canonical address book already contains at least one genuinely approved originator and make `create` accept a caller that is neither the approved originator nor an admin/guardian, breaking the rule that the `originator` parameter and the effective caller identity should never diverge for an unprivileged transaction and leading to Accounting issue in Loans caused by unauthorized lifecycle initialization?

## Target
- File/function: contracts/Loans.sol / create
- Entrypoint: Loans.create(address,address,address,address,int128,uint48)
- Attacker controls: the same attacker-controlled address reused across multiple loan roles
- Exploit idea: make `create` accept a caller that is neither the approved originator nor an admin/guardian
- Invariant to test: the `originator` parameter and the effective caller identity should never diverge for an unprivileged transaction
- Expected Immunefi impact: Accounting issue in Loans caused by unauthorized lifecycle initialization
- Fast validation: Fuzz `msg.sender`, the `originator` parameter, and address-book contents, then assert every successful create binds to a canonically approved originator only.
