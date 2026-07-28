# Q3673: TrustedSpender route and allowance scoping: shared delegate / route widening / per-safe route binding

## Question
Can an unprivileged delegate of its own Safe, without any rights on a victim Safe enter through `TrustedSpender.executeTransfer(...) and executeNFTTransfer(...)` with the same delegate address authorized on one Safe but not another Safe with similar recipients while the route is configured with an infinite ERC20 allowance and long validity and spend to a recipient or token/collection route that was never approved for that Safe, breaking the rule that delegate rights and allowances should bind to one exact `(safe, token-or-collection, recipient)` route only and leading to Bypass of intended permissions and allowance scoping?

## Target
- File/function: contracts/TrustedSpender.sol / executeTransfer, executeNFTTransfer, setAllowance, setNFTAllowance
- Entrypoint: TrustedSpender.executeTransfer(...) and executeNFTTransfer(...)
- Attacker controls: the same delegate address authorized on one Safe but not another Safe with similar recipients
- Exploit idea: spend to a recipient or token/collection route that was never approved for that Safe
- Invariant to test: delegate rights and allowances should bind to one exact `(safe, token-or-collection, recipient)` route only
- Expected Immunefi impact: Bypass of intended permissions and allowance scoping
- Fast validation: Fuzz allowance expiry at the block boundary and ensure no post-expiry transfer succeeds for ERC20 or ERC721 routes.
