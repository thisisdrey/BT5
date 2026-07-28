# Q0009: Loans create authorization: self-book spoof / role confusion / canonical gate

## Question
Can an unprivileged caller with no guardian, admin, originator, or servicer powers enter through `Loans.create(address,address,address,address,int128,uint48)` with only a self-curated address book plus attacker-controlled borrower, investor, and servicer addresses while the canonical address book already contains at least one genuinely approved originator and reuse one role approval context to satisfy a different role check and initialize a loan with unauthorized parties, breaking the rule that only the canonical `address(this)` originator book should authorize loan creation and leading to Loans NFT being minted into an unauthorized economic context that can later harm another user?

## Target
- File/function: contracts/Loans.sol / create
- Entrypoint: Loans.create(address,address,address,address,int128,uint48)
- Attacker controls: only a self-curated address book plus attacker-controlled borrower, investor, and servicer addresses
- Exploit idea: reuse one role approval context to satisfy a different role check and initialize a loan with unauthorized parties
- Invariant to test: only the canonical `address(this)` originator book should authorize loan creation
- Expected Immunefi impact: Loans NFT being minted into an unauthorized economic context that can later harm another user
- Fast validation: Build a minimal scenario where a toxic loan would later be fundable or priceable, and assert the creation step itself cannot be reached by an unprivileged caller.
