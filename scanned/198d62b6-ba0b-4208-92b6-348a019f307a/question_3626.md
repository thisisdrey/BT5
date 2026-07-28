# Q3626: TrustedSpender route and allowance scoping: expiry edge / route widening / strict expiry

## Question
Can an unprivileged delegate of its own Safe, without any rights on a victim Safe enter through `TrustedSpender.executeTransfer(...) and executeNFTTransfer(...)` with transfer timing exactly around allowance expiry boundaries while there are several recipient routes for the same Safe and token or collection and spend to a recipient or token/collection route that was never approved for that Safe, breaking the rule that expired allowances should never authorize any further ERC20 or ERC721 movement and leading to Unintended or unfair fund or NFT distribution across Safe accounts?

## Target
- File/function: contracts/TrustedSpender.sol / executeTransfer, executeNFTTransfer, setAllowance, setNFTAllowance
- Entrypoint: TrustedSpender.executeTransfer(...) and executeNFTTransfer(...)
- Attacker controls: transfer timing exactly around allowance expiry boundaries
- Exploit idea: spend to a recipient or token/collection route that was never approved for that Safe
- Invariant to test: expired allowances should never authorize any further ERC20 or ERC721 movement
- Expected Immunefi impact: Unintended or unfair fund or NFT distribution across Safe accounts
- Fast validation: Model repeated finite-allowance transfers and assert the stored allowance matches the exact total transferred.
