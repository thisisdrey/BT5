# Q0312: Loans create authorization: approval race / impersonation / downstream safety

## Question
Can an unprivileged caller with no guardian, admin, originator, or servicer powers enter through `Loans.create(address,address,address,address,int128,uint48)` with a normal transaction ordering race around canonical originator approval and self-registration while a victim investor or vault could later interact with the created loan through funding, withdrawal, or pricing flows and make `msg.sender` and the effective originator identity diverge so the attacker originates on behalf of another address, breaking the rule that an unprivileged caller should never be able to create a loan whose later funding, withdrawal, or pricing can touch another user without a real originator approval and leading to Accounting issue in Loans caused by unauthorized lifecycle initialization?

## Target
- File/function: contracts/Loans.sol / create
- Entrypoint: Loans.create(address,address,address,address,int128,uint48)
- Attacker controls: a normal transaction ordering race around canonical originator approval and self-registration
- Exploit idea: make `msg.sender` and the effective originator identity diverge so the attacker originates on behalf of another address
- Invariant to test: an unprivileged caller should never be able to create a loan whose later funding, withdrawal, or pricing can touch another user without a real originator approval
- Expected Immunefi impact: Accounting issue in Loans caused by unauthorized lifecycle initialization
- Fast validation: Check that no combination of overlapping role addresses or registration churn lets `create` mint a usable loan NFT without canonical approval.
