# Q0261: Loans create authorization: approval race / impersonation / canonical gate

## Question
Can an unprivileged caller with no guardian, admin, originator, or servicer powers enter through `Loans.create(address,address,address,address,int128,uint48)` with a normal transaction ordering race around canonical originator approval and self-registration while the canonical address book already contains at least one genuinely approved originator and make `msg.sender` and the effective originator identity diverge so the attacker originates on behalf of another address, breaking the rule that only the canonical `address(this)` originator book should authorize loan creation and leading to Unintended or unfair fund distribution once the unauthorized loan enters normal funding and withdrawal flows?

## Target
- File/function: contracts/Loans.sol / create
- Entrypoint: Loans.create(address,address,address,address,int128,uint48)
- Attacker controls: a normal transaction ordering race around canonical originator approval and self-registration
- Exploit idea: make `msg.sender` and the effective originator identity diverge so the attacker originates on behalf of another address
- Invariant to test: only the canonical `address(this)` originator book should authorize loan creation
- Expected Immunefi impact: Unintended or unfair fund distribution once the unauthorized loan enters normal funding and withdrawal flows
- Fast validation: Check that no combination of overlapping role addresses or registration churn lets `create` mint a usable loan NFT without canonical approval.
