# Q0101: Loans create authorization: originator mismatch / impersonation / canonical gate

## Question
Can an unprivileged caller with no guardian, admin, originator, or servicer powers enter through `Loans.create(address,address,address,address,int128,uint48)` with a fake `originator` parameter that names another address while the attacker remains `msg.sender` while the target counterparty addresses are registered and unregistered across attacker-controlled books in nearby transactions and make `msg.sender` and the effective originator identity diverge so the attacker originates on behalf of another address, breaking the rule that only the canonical `address(this)` originator book should authorize loan creation and leading to Bypass of intended permissions and role-based access control?

## Target
- File/function: contracts/Loans.sol / create
- Entrypoint: Loans.create(address,address,address,address,int128,uint48)
- Attacker controls: a fake `originator` parameter that names another address while the attacker remains `msg.sender`
- Exploit idea: make `msg.sender` and the effective originator identity diverge so the attacker originates on behalf of another address
- Invariant to test: only the canonical `address(this)` originator book should authorize loan creation
- Expected Immunefi impact: Bypass of intended permissions and role-based access control
- Fast validation: Forge test a caller with only self-book registrations and assert `create` cannot succeed unless the canonical originator book explicitly approves the caller.
