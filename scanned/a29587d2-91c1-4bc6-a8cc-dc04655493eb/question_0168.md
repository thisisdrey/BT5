# Q0168: Loans create authorization: role overlap / impersonation / downstream safety

## Question
Can an unprivileged caller with no guardian, admin, originator, or servicer powers enter through `Loans.create(address,address,address,address,int128,uint48)` with the same attacker-controlled address reused across multiple loan roles while the target counterparty addresses are registered and unregistered across attacker-controlled books in nearby transactions and make `msg.sender` and the effective originator identity diverge so the attacker originates on behalf of another address, breaking the rule that an unprivileged caller should never be able to create a loan whose later funding, withdrawal, or pricing can touch another user without a real originator approval and leading to Bypass of intended permissions and role-based access control?

## Target
- File/function: contracts/Loans.sol / create
- Entrypoint: Loans.create(address,address,address,address,int128,uint48)
- Attacker controls: the same attacker-controlled address reused across multiple loan roles
- Exploit idea: make `msg.sender` and the effective originator identity diverge so the attacker originates on behalf of another address
- Invariant to test: an unprivileged caller should never be able to create a loan whose later funding, withdrawal, or pricing can touch another user without a real originator approval
- Expected Immunefi impact: Bypass of intended permissions and role-based access control
- Fast validation: Forge test a caller with only self-book registrations and assert `create` cannot succeed unless the canonical originator book explicitly approves the caller.
