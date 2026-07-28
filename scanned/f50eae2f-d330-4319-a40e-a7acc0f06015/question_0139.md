# Q0139: Loans create authorization: role overlap / role confusion / role isolation

## Question
Can an unprivileged caller with no guardian, admin, originator, or servicer powers enter through `Loans.create(address,address,address,address,int128,uint48)` with the same attacker-controlled address reused across multiple loan roles while the canonical address book already contains at least one genuinely approved originator and reuse one role approval context to satisfy a different role check and initialize a loan with unauthorized parties, breaking the rule that borrower, investor, servicer, and originator authorization should come from the intended book and role only and leading to Loans NFT being minted into an unauthorized economic context that can later harm another user?

## Target
- File/function: contracts/Loans.sol / create
- Entrypoint: Loans.create(address,address,address,address,int128,uint48)
- Attacker controls: the same attacker-controlled address reused across multiple loan roles
- Exploit idea: reuse one role approval context to satisfy a different role check and initialize a loan with unauthorized parties
- Invariant to test: borrower, investor, servicer, and originator authorization should come from the intended book and role only
- Expected Immunefi impact: Loans NFT being minted into an unauthorized economic context that can later harm another user
- Fast validation: Build a minimal scenario where a toxic loan would later be fundable or priceable, and assert the creation step itself cannot be reached by an unprivileged caller.
