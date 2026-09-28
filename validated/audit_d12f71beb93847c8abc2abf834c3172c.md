### Title
Failed inbound LayerZero messages can be replayed on a hard-forked chain, minting unbacked synthetic tokens twice — (File: contracts/dependencies/@layerzerolabs/solidity-examples/contracts-upgradeable/lzApp/NonblockingLzAppUpgradeable.sol)

### Summary
Metronome's `ProxyOFT` inherits `NonblockingLzAppUpgradeable`, which stores failed inbound LayerZero messages in `failedMessages` and lets *anyone* re-execute them via the permissionless `retryMessage` (and `retryOFTReceived` in `ComposableOFTCoreUpgradeable`). Neither function — nor the downstream `_sendAck` → `ProxyOFT._creditTo` → `syntheticToken.mint` path — validates `block.chainid` (a `checkFork`-style check). If the destination chain hard-forks while stored messages are pending (or while the LayerZero endpoint still holds inbound payloads awaiting delivery), the same message can be executed on both sides of the fork, minting `msUSD`/`msETH` on the fork chain that was never burned on the source chain — the same class as the Omni bridge PoW-fork replay.

### Finding Description
The inbound path is:

1. `LzAppUpgradeable.lzReceive` (only callable by the LZ endpoint) → `_blockingLzReceive` → `NonblockingLzAppUpgradeable._blockingLzReceive`, which try/catches `nonblockingLzReceive` and on failure calls `_storeFailedMessage`, recording `failedMessages[_srcChainId][_srcAddress][_nonce] = keccak256(_payload)` at `NonblockingLzAppUpgradeable.sol:37-39`.
2. `retryMessage` at `NonblockingLzAppUpgradeable.sol:51-61` is `public payable`, clears the stored hash, and re-runs `_nonblockingLzReceive`, which decodes `PT_SEND` and calls `_sendAck` → `ProxyOFT._creditTo` (`contracts/ProxyOFT.sol:86-93`) → `syntheticToken.mint(toAddress_, amount_)`.
3. `retryOFTReceived` at `ComposableOFTCoreUpgradeable.sol:76-94` behaves identically for failed `onOFTReceived` callbacks.

Nothing in this path binds execution to the intended chain: `_sendAck` ignores `_srcChainId` for authorization (`OFTCoreUpgradeable.sol:103-110`), `_creditTo` ignores `srcChainId_` entirely, and there is no `block.chainid`/`checkFork` equivalent anywhere in `ProxyOFT` or the LZ app chain. The `_srcChainId` parameter is the LayerZero *source* chain id, not the executing chain.

After a contentious hard fork of a chain where `ProxyOFT` is deployed (mainnet, Optimism, Base, Hemi, Swell, Plasma deployments exist), the forked state includes the same `failedMessages` entries and the same LZ endpoint inbound queue. An unprivileged attacker can call `retryMessage`/`retryOFTReceived` on the fork to mint synthetic tokens on the fork chain, then dump them into the fork's AMM pools for the fork's native asset — exactly the Omni-bridge-style extraction. Conversely, messages delivered via the endpoint on one fork can also be delivered on the other, breaking bridge conservation: a burn on the source chain results in two mints.

### Impact Explanation
Bridge conservation invariant breaks: `burn` on source chain ↔ `mint` on destination chain is no longer 1:1 across the fork boundary. Unbacked `msUSD`/`msETH` minted on a value-bearing fork can be swapped for fork-native assets or deposited into the fork's Metronome pools as collateral, draining real liquidity. Direct theft of value proportional to the queued/failed bridge volume on the fork.

### Likelihood Explanation
Low-to-moderate: requires (a) a hard fork of a chain hosting `ProxyOFT` where the fork retains economic value and live AMM/pool liquidity (Ethereum PoW-style), and (b) pending inbound messages — either stored `failedMessages`/`failedOFTReceivedMessages` entries or endpoint-held payloads — at fork time. Both conditions occurred in the Omni incident. The `sendAndCall` path is disabled (`SendAndCallNotAllowed`), which narrows the surface to `retryMessage`/`retryOFTReceived` and direct endpoint delivery, but does not eliminate it. No privileged actor is needed for the retry calls; the LayerZero endpoint itself is assumed honest (it delivers the same payload on both forks by design, not by malice).

### Recommendation
Bind inbound execution to the chain: record the expected `block.chainid` (or a chain-identifier salted at initialization) and check it in `lzReceive`, `retryMessage`, and `retryOFTReceived` — e.g., include `block.chainid` in the stored `failedMessages` hash so a payload stored pre-fork cannot be replayed on a chain with a different `chainid`. As a defense-in-depth measure, `ProxyOFT._creditTo` should verify the executing chain is the deployment's intended chain id.

### Proof of Concept
Foundry fork sketch (two forks from identical pre-fork state):

```solidity
// Setup: mainnet fork at block N with ProxyOFT + a stored failed message.
// 1. On fork A (canonical): attacker calls
//    proxyOFT.retryMessage(srcChainId, srcAddress, nonce, payload);
//    -> msUSD.mint(attackerTo, amount) succeeds.
// 2. On fork B (hard-fork twin, different block.chainid):
//    same call, same arguments -> mint succeeds AGAIN because
//    failedMessages mapping was duplicated and no chainid check exists.
// assert(msUSD_A.balanceOf(to) == amount);
// assert(msUSD_B.balanceOf(to) == amount); // unbacked second mint
```

To create the stored message pre-fork, deliver a `PT_SEND` payload whose mint reverts (e.g., `to` is a contract that triggers a revert in a mint hook / paused `syntheticToken`), causing `_storeFailedMessage` to record it; unpause after the fork and replay on both forks via `retryMessage`.