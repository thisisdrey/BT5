# Q0242: Loans create authorization: contract receiver / unauthorized create / caller binding

## Question
Can an unprivileged caller with no guardian, admin, originator, or servicer powers enter through `Loans.create(address,address,address,address,int128,uint48)` with an attacker-controlled contract as the proposed investor plus separate EOAs for the other roles while a victim investor or vault could later interact with the created loan through funding, withdrawal, or pricing flows and make `create` accept a caller that is neither the approved originator nor an admin/guardian, breaking the rule that the `originator` parameter and the effective caller identity should never diverge for an unprivileged transaction and leading to Accounting issue in Loans caused by unauthorized lifecycle initialization?

## Target
- File/function: contracts/Loans.sol / create
- Entrypoint: Loans.create(address,address,address,address,int128,uint48)
- Attacker controls: an attacker-controlled contract as the proposed investor plus separate EOAs for the other roles
- Exploit idea: make `create` accept a caller that is neither the approved originator nor an admin/guardian
- Invariant to test: the `originator` parameter and the effective caller identity should never diverge for an unprivileged transaction
- Expected Immunefi impact: Accounting issue in Loans caused by unauthorized lifecycle initialization
- Fast validation: Check that no combination of overlapping role addresses or registration churn lets `create` mint a usable loan NFT without canonical approval.
