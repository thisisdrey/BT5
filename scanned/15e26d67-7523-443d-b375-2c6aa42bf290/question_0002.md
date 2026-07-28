# Q0002: Loans create authorization: self-book spoof / unauthorized create / caller binding

## Question
Can an unprivileged caller with no guardian, admin, originator, or servicer powers enter through `Loans.create(address,address,address,address,int128,uint48)` with only a self-curated address book plus attacker-controlled borrower, investor, and servicer addresses while the canonical address book already contains at least one genuinely approved originator and make `create` accept a caller that is neither the approved originator nor an admin/guardian, breaking the rule that the `originator` parameter and the effective caller identity should never diverge for an unprivileged transaction and leading to Unintended or unfair fund distribution once the unauthorized loan enters normal funding and withdrawal flows?

## Target
- File/function: contracts/Loans.sol / create
- Entrypoint: Loans.create(address,address,address,address,int128,uint48)
- Attacker controls: only a self-curated address book plus attacker-controlled borrower, investor, and servicer addresses
- Exploit idea: make `create` accept a caller that is neither the approved originator nor an admin/guardian
- Invariant to test: the `originator` parameter and the effective caller identity should never diverge for an unprivileged transaction
- Expected Immunefi impact: Unintended or unfair fund distribution once the unauthorized loan enters normal funding and withdrawal flows
- Fast validation: Check that no combination of overlapping role addresses or registration churn lets `create` mint a usable loan NFT without canonical approval.
