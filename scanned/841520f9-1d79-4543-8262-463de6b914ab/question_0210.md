# Q0210: Loans create authorization: contract receiver / unauthorized create / caller binding

## Question
Can an unprivileged caller with no guardian, admin, originator, or servicer powers enter through `Loans.create(address,address,address,address,int128,uint48)` with an attacker-controlled contract as the proposed investor plus separate EOAs for the other roles while the attacker has only self-registered role bits in its own address book and no canonical approvals and make `create` accept a caller that is neither the approved originator nor an admin/guardian, breaking the rule that the `originator` parameter and the effective caller identity should never diverge for an unprivileged transaction and leading to Unintended or unfair fund distribution once the unauthorized loan enters normal funding and withdrawal flows?

## Target
- File/function: contracts/Loans.sol / create
- Entrypoint: Loans.create(address,address,address,address,int128,uint48)
- Attacker controls: an attacker-controlled contract as the proposed investor plus separate EOAs for the other roles
- Exploit idea: make `create` accept a caller that is neither the approved originator nor an admin/guardian
- Invariant to test: the `originator` parameter and the effective caller identity should never diverge for an unprivileged transaction
- Expected Immunefi impact: Unintended or unfair fund distribution once the unauthorized loan enters normal funding and withdrawal flows
- Fast validation: Fuzz `msg.sender`, the `originator` parameter, and address-book contents, then assert every successful create binds to a canonically approved originator only.
