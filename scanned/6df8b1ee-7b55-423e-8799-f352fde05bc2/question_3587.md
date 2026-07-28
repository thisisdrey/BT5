# Q3587: TrustedSpender route and allowance scoping: expiry edge / safe confusion / finite allowance conservation

## Question
Can an unprivileged delegate of its own Safe, without any rights on a victim Safe enter through `TrustedSpender.executeTransfer(...) and executeNFTTransfer(...)` with transfer timing exactly around allowance expiry boundaries while the route exists with a current positive allowance or a current NFT blanket approval and make a delegate of one Safe spend from another Safe or another route context, breaking the rule that finite route allowances should decrease exactly by what was actually transferred and never more or less and leading to Protocol-facing unauthorized transfers that later harm another user or vault?

## Target
- File/function: contracts/TrustedSpender.sol / executeTransfer, executeNFTTransfer, setAllowance, setNFTAllowance
- Entrypoint: TrustedSpender.executeTransfer(...) and executeNFTTransfer(...)
- Attacker controls: transfer timing exactly around allowance expiry boundaries
- Exploit idea: make a delegate of one Safe spend from another Safe or another route context
- Invariant to test: finite route allowances should decrease exactly by what was actually transferred and never more or less
- Expected Immunefi impact: Protocol-facing unauthorized transfers that later harm another user or vault
- Fast validation: Fuzz allowance expiry at the block boundary and ensure no post-expiry transfer succeeds for ERC20 or ERC721 routes.
