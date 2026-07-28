# Q3733: TrustedSpender route and allowance scoping: nft route / expiry bypass / per-safe route binding

## Question
Can an unprivileged delegate of its own Safe, without any rights on a victim Safe enter through `TrustedSpender.executeTransfer(...) and executeNFTTransfer(...)` with collection, from, to, and tokenId across blanket NFT allowances while the route is configured with an infinite ERC20 allowance and long validity and spend after expiry or across an expiry edge when the route should no longer be valid, breaking the rule that delegate rights and allowances should bind to one exact `(safe, token-or-collection, recipient)` route only and leading to Bypass of intended permissions and allowance scoping?

## Target
- File/function: contracts/TrustedSpender.sol / executeTransfer, executeNFTTransfer, setAllowance, setNFTAllowance
- Entrypoint: TrustedSpender.executeTransfer(...) and executeNFTTransfer(...)
- Attacker controls: collection, from, to, and tokenId across blanket NFT allowances
- Exploit idea: spend after expiry or across an expiry edge when the route should no longer be valid
- Invariant to test: delegate rights and allowances should bind to one exact `(safe, token-or-collection, recipient)` route only
- Expected Immunefi impact: Bypass of intended permissions and allowance scoping
- Fast validation: Fuzz allowance expiry at the block boundary and ensure no post-expiry transfer succeeds for ERC20 or ERC721 routes.
