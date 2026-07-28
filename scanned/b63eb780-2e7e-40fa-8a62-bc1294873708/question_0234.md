# Q0234: Loans create authorization: contract receiver / role confusion / caller binding

## Question
Can an unprivileged caller with no guardian, admin, originator, or servicer powers enter through `Loans.create(address,address,address,address,int128,uint48)` with an attacker-controlled contract as the proposed investor plus separate EOAs for the other roles while the target counterparty addresses are registered and unregistered across attacker-controlled books in nearby transactions and reuse one role approval context to satisfy a different role check and initialize a loan with unauthorized parties, breaking the rule that the `originator` parameter and the effective caller identity should never diverge for an unprivileged transaction and leading to Bypass of intended permissions and role-based access control?

## Target
- File/function: contracts/Loans.sol / create
- Entrypoint: Loans.create(address,address,address,address,int128,uint48)
- Attacker controls: an attacker-controlled contract as the proposed investor plus separate EOAs for the other roles
- Exploit idea: reuse one role approval context to satisfy a different role check and initialize a loan with unauthorized parties
- Invariant to test: the `originator` parameter and the effective caller identity should never diverge for an unprivileged transaction
- Expected Immunefi impact: Bypass of intended permissions and role-based access control
- Fast validation: Forge test a caller with only self-book registrations and assert `create` cannot succeed unless the canonical originator book explicitly approves the caller.
