# Q0111: Loans create authorization: originator mismatch / toxic loan seed / role isolation

## Question
Can an unprivileged caller with no guardian, admin, originator, or servicer powers enter through `Loans.create(address,address,address,address,int128,uint48)` with a fake `originator` parameter that names another address while the attacker remains `msg.sender` while the target counterparty addresses are registered and unregistered across attacker-controlled books in nearby transactions and seed a loan that looks valid on-chain even though the attacker never passed the intended canonical-originator gate, breaking the rule that borrower, investor, servicer, and originator authorization should come from the intended book and role only and leading to Bypass of intended permissions and role-based access control?

## Target
- File/function: contracts/Loans.sol / create
- Entrypoint: Loans.create(address,address,address,address,int128,uint48)
- Attacker controls: a fake `originator` parameter that names another address while the attacker remains `msg.sender`
- Exploit idea: seed a loan that looks valid on-chain even though the attacker never passed the intended canonical-originator gate
- Invariant to test: borrower, investor, servicer, and originator authorization should come from the intended book and role only
- Expected Immunefi impact: Bypass of intended permissions and role-based access control
- Fast validation: Forge test a caller with only self-book registrations and assert `create` cannot succeed unless the canonical originator book explicitly approves the caller.
