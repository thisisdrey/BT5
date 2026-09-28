### Title
Bridged-in LayerZero messages can be frozen indefinitely by saturating `maxBridgedInSupply`, with no revert path on the source chain - (File: contracts/ProxyOFT.sol)

### Summary
`ProxyOFT._creditTo` mints the bridged amount via `SyntheticToken.mint`, which reverts with `SurpassMaxBridgingSupply` when `bridgedInSupply() > maxBridgedInSupply`. When the destination-side mint reverts, the LayerZero nonblocking app stores the message hash in `failedMessages`, but the tokens were already burned on the source chain by `_debitFrom`. An unprivileged attacker can keep the bridged-in supply saturated (by bridging tokens in themselves and re-bridging after any bridge-out), so every `retryMessage` keeps reverting and the victim's burned funds are never re-minted — with no mechanism to reclaim them on the source chain.

### Finding Description
On send, `ProxyOFT._debitFrom` burns `amount_` from the sender on the source chain (`SyntheticToken.burn`, which also increments `totalBridgedOut`). On the destination chain, `lzReceive` → `_blockingLzReceive` → `nonblockingLzReceive` → `_nonblockingLzReceive` → `_sendAck` → `ProxyOFT._creditTo` → `syntheticToken.mint(toAddress_, amount_)`.

In `SyntheticToken._mint` (contracts/SyntheticToken.sol:338-340), when the caller is the ProxyOFT, `totalBridgedIn += amount_` and it reverts with `SurpassMaxBridgingSupply` if `bridgedInSupply() > maxBridgedInSupply`. `bridgedInSupply()` is `totalBridgedIn - totalBridgedOut` — a quantity any user can increase simply by bridging tokens into this chain.

If the mint reverts, `_blockingLzReceive` catches the failure and stores `keccak256(_payload)` in `failedMessages[srcChainId][srcAddress][nonce]` (NonblockingLzAppUpgradeable.sol:37-40). Recovery relies solely on `retryMessage` (lines 51-60), which re-executes `_nonblockingLzReceive` and reverts again as long as the cap is saturated. There is no timeout and no "return to sender" path: the source-chain burn is final.

This mirrors the reported bug class — like the Wormhole guardian-set expiry, a condition on the receiving chain that was valid at send time can become invalid before delivery, permanently blocking acceptance with no revert on the sending chain. Here the mutable condition (`bridgedInSupply` vs. `maxBridgedInSupply`) is attacker-influenceable rather than governance-time-based.

### Impact Explanation
A victim's cross-chain `sendFrom` burns their synthetic tokens on the source chain. If the destination mint keeps failing, the user holds nothing on either chain: the source burn cannot be undone and the destination mint never completes. As long as an attacker keeps `bridgedInSupply` at the cap (re-bridging in after any bridge-out frees room), retries revert indefinitely — a prolonged, attacker-controlled freezing of user funds; if the cap is never raised, effectively permanent.

### Likelihood Explanation
Requires a configured `maxBridgedInSupply` close enough to utilization that an attacker can bridge in the remainder. The attacker needs capital equal to the gap between current `bridgedInSupply` and the cap (and to re-fill after others bridge out), which bounds feasibility but is a pure capital cost — no privileged role needed. Front-running is trivially achievable since LayerZero delivery is observable and the attacker only needs their inbound transfers to land before the victim's message executes.

### Recommendation
Add a source-chain recovery path: e.g., a `refundMessage`/revert flow so a message that fails (or stays undelivered past a deadline) re-mints the burned amount on the source chain. Alternatively, credit bridged amounts into a claimable balance rather than minting directly, so transient cap saturation does not block delivery; or exclude the accounting failure mode by checking remaining headroom at send time on the destination's reported supply.

### Proof of Concept
```solidity
// Foundry fork test on the destination chain (e.g., mainnet fork of msUSD ProxyOFT)
function test_BridgedInCapGriefing() public {
    // victim calls ProxyOFT.sendFrom on SOURCE; LZ delivers payload P on DST
    // attacker bridges in X msUSD (or simulates by minting via proxyOFT path)
    // such that syntheticToken.bridgedInSupply() + victimAmount > maxBridgedInSupply

    // Simulate failed delivery: endpoint calls lzReceive -> mint reverts ->
    // failedMessages[srcChainId][srcAddr][nonce] = keccak256(P)
    vm.prank(address(lzEndpoint));
    proxyOFT.lzReceive(srcChainId, srcAddr, nonce, P);
    assert(proxyOFT.failedMessages(srcChainId, srcAddr, nonce) != bytes32(0));

    // Any retry reverts while cap is saturated
    vm.expectRevert(); // SurpassMaxBridgingSupply inside _nonblockingLzReceive
    proxyOFT.retryMessage(srcChainId, srcAddr, nonce, P);

    // victim's tokens were burned on source; not minted here;
    // attacker re-bridges in after any bridge-out to keep the cap saturated
    // => funds remain frozen with no source-chain recovery function
}
```
Key references: `ProxyOFT._debitFrom` burn (contracts/ProxyOFT.sol:70-83), `ProxyOFT._creditTo` mint (lines 86-93), `SyntheticToken._mint` cap check (contracts/SyntheticToken.sol:338-340), `bridgedInSupply` net accounting (lines 160-167), failed-message storage and `retryMessage` (contracts/dependencies/@layerzerolabs/solidity-examples/contracts-upgradeable/lzApp/NonblockingLzAppUpgradeable.sol:37-60).