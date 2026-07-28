# Q0249: Loans create authorization: contract receiver / role confusion / canonical gate

## Question
Can an unprivileged caller with no guardian, admin, originator, or servicer powers enter through `Loans.create(address,address,address,address,int128,uint48)` with an attacker-controlled contract as the proposed investor plus separate EOAs for the other roles while a victim investor or vault could later interact with the created loan through funding, withdrawal, or pricing flows and reuse one role approval context to satisfy a different role check and initialize a loan with unauthorized parties, breaking the rule that only the canonical `address(this)` originator book should authorize loan creation and leading to Bypass of intended permissions and role-based access control?

## Target
- File/function: contracts/Loans.sol / create
- Entrypoint: Loans.create(address,address,address,address,int128,uint48)
- Attacker controls: an attacker-controlled contract as the proposed investor plus separate EOAs for the other roles
- Exploit idea: reuse one role approval context to satisfy a different role check and initialize a loan with unauthorized parties
- Invariant to test: only the canonical `address(this)` originator book should authorize loan creation
- Expected Immunefi impact: Bypass of intended permissions and role-based access control
- Fast validation: Build a minimal scenario where a toxic loan would later be fundable or priceable, and assert the creation step itself cannot be reached by an unprivileged caller.
