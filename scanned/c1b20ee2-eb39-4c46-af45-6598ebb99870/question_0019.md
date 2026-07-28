# Q0019: Loans create authorization: self-book spoof / unauthorized create / role isolation

## Question
Can an unprivileged caller with no guardian, admin, originator, or servicer powers enter through `Loans.create(address,address,address,address,int128,uint48)` with only a self-curated address book plus attacker-controlled borrower, investor, and servicer addresses while the attacker has only self-registered role bits in its own address book and no canonical approvals and make `create` accept a caller that is neither the approved originator nor an admin/guardian, breaking the rule that borrower, investor, servicer, and originator authorization should come from the intended book and role only and leading to Accounting issue in Loans caused by unauthorized lifecycle initialization?

## Target
- File/function: contracts/Loans.sol / create
- Entrypoint: Loans.create(address,address,address,address,int128,uint48)
- Attacker controls: only a self-curated address book plus attacker-controlled borrower, investor, and servicer addresses
- Exploit idea: make `create` accept a caller that is neither the approved originator nor an admin/guardian
- Invariant to test: borrower, investor, servicer, and originator authorization should come from the intended book and role only
- Expected Immunefi impact: Accounting issue in Loans caused by unauthorized lifecycle initialization
- Fast validation: Check that no combination of overlapping role addresses or registration churn lets `create` mint a usable loan NFT without canonical approval.
