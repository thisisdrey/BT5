# Q0160: Loans create authorization: role overlap / toxic loan seed / downstream safety

## Question
Can an unprivileged caller with no guardian, admin, originator, or servicer powers enter through `Loans.create(address,address,address,address,int128,uint48)` with the same attacker-controlled address reused across multiple loan roles while the attacker has only self-registered role bits in its own address book and no canonical approvals and seed a loan that looks valid on-chain even though the attacker never passed the intended canonical-originator gate, breaking the rule that an unprivileged caller should never be able to create a loan whose later funding, withdrawal, or pricing can touch another user without a real originator approval and leading to Unintended or unfair fund distribution once the unauthorized loan enters normal funding and withdrawal flows?

## Target
- File/function: contracts/Loans.sol / create
- Entrypoint: Loans.create(address,address,address,address,int128,uint48)
- Attacker controls: the same attacker-controlled address reused across multiple loan roles
- Exploit idea: seed a loan that looks valid on-chain even though the attacker never passed the intended canonical-originator gate
- Invariant to test: an unprivileged caller should never be able to create a loan whose later funding, withdrawal, or pricing can touch another user without a real originator approval
- Expected Immunefi impact: Unintended or unfair fund distribution once the unauthorized loan enters normal funding and withdrawal flows
- Fast validation: Fuzz `msg.sender`, the `originator` parameter, and address-book contents, then assert every successful create binds to a canonically approved originator only.
