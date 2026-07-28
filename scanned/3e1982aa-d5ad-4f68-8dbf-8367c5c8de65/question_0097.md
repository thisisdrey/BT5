# Q0097: Loans create authorization: originator mismatch / unauthorized create / canonical gate

## Question
Can an unprivileged caller with no guardian, admin, originator, or servicer powers enter through `Loans.create(address,address,address,address,int128,uint48)` with a fake `originator` parameter that names another address while the attacker remains `msg.sender` while the target counterparty addresses are registered and unregistered across attacker-controlled books in nearby transactions and make `create` accept a caller that is neither the approved originator nor an admin/guardian, breaking the rule that only the canonical `address(this)` originator book should authorize loan creation and leading to Accounting issue in Loans caused by unauthorized lifecycle initialization?

## Target
- File/function: contracts/Loans.sol / create
- Entrypoint: Loans.create(address,address,address,address,int128,uint48)
- Attacker controls: a fake `originator` parameter that names another address while the attacker remains `msg.sender`
- Exploit idea: make `create` accept a caller that is neither the approved originator nor an admin/guardian
- Invariant to test: only the canonical `address(this)` originator book should authorize loan creation
- Expected Immunefi impact: Accounting issue in Loans caused by unauthorized lifecycle initialization
- Fast validation: Fuzz `msg.sender`, the `originator` parameter, and address-book contents, then assert every successful create binds to a canonically approved originator only.
