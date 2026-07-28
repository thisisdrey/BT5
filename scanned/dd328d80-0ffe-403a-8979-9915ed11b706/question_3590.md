# Q3590: TrustedSpender route and allowance scoping: expiry edge / expiry bypass / strict expiry

## Question
Can an unprivileged delegate of its own Safe, without any rights on a victim Safe enter through `TrustedSpender.executeTransfer(...) and executeNFTTransfer(...)` with transfer timing exactly around allowance expiry boundaries while the route exists with a current positive allowance or a current NFT blanket approval and spend after expiry or across an expiry edge when the route should no longer be valid, breaking the rule that expired allowances should never authorize any further ERC20 or ERC721 movement and leading to Protocol-facing unauthorized transfers that later harm another user or vault?

## Target
- File/function: contracts/TrustedSpender.sol / executeTransfer, executeNFTTransfer, setAllowance, setNFTAllowance
- Entrypoint: TrustedSpender.executeTransfer(...) and executeNFTTransfer(...)
- Attacker controls: transfer timing exactly around allowance expiry boundaries
- Exploit idea: spend after expiry or across an expiry edge when the route should no longer be valid
- Invariant to test: expired allowances should never authorize any further ERC20 or ERC721 movement
- Expected Immunefi impact: Protocol-facing unauthorized transfers that later harm another user or vault
- Fast validation: Fuzz allowance expiry at the block boundary and ensure no post-expiry transfer succeeds for ERC20 or ERC721 routes.
