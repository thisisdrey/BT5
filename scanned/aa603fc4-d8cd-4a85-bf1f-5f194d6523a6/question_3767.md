# Q3767: TrustedSpender route and allowance scoping: nft route / expiry bypass / finite allowance conservation

## Question
Can an unprivileged delegate of its own Safe, without any rights on a victim Safe enter through `TrustedSpender.executeTransfer(...) and executeNFTTransfer(...)` with collection, from, to, and tokenId across blanket NFT allowances while another Safe has a similar delegate or recipient topology but no matching route and spend after expiry or across an expiry edge when the route should no longer be valid, breaking the rule that finite route allowances should decrease exactly by what was actually transferred and never more or less and leading to Bypass of intended permissions and allowance scoping?

## Target
- File/function: contracts/TrustedSpender.sol / executeTransfer, executeNFTTransfer, setAllowance, setNFTAllowance
- Entrypoint: TrustedSpender.executeTransfer(...) and executeNFTTransfer(...)
- Attacker controls: collection, from, to, and tokenId across blanket NFT allowances
- Exploit idea: spend after expiry or across an expiry edge when the route should no longer be valid
- Invariant to test: finite route allowances should decrease exactly by what was actually transferred and never more or less
- Expected Immunefi impact: Bypass of intended permissions and allowance scoping
- Fast validation: Fuzz allowance expiry at the block boundary and ensure no post-expiry transfer succeeds for ERC20 or ERC721 routes.
