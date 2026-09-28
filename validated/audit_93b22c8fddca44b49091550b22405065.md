### Title
Bridged-in mint blocked by cap saturation permanently strands burned funds in `failedMessages` — error path never restores source-chain accounting - ([File: contracts/SyntheticToken.sol](contracts/SyntheticToken.sol))

### Summary
The external report class is "a later failure path does not undo a resource enabled earlier." In Metronome, `ProxyOFT._debitFrom` burns the synthetic token and permanently increments `totalBridgedOut` on the source chain, while `ProxyOFT._creditTo` mints on the destination chain inside LayerZero's nonblocking receive. If `SyntheticToken._mint` reverts (e.g., `SurpassMaxBridgingSupply`), the failure is cached in `failedMessages` and only a later permissionless `retryMessage` can complete it — there is no compensating path on the source chain. An unprivileged attacker can keep `bridgedInSupply()` saturated at `maxBridgedInSupply` so every retry reverts, freezing the victim's bridged funds indefinitely.

### Finding Description
- `ProxyOFT._debitFrom` calls `syntheticToken.burn(from_, amount_)` (`contracts/ProxyOFT.sol:70-83`).
- `SyntheticToken._burn` increments `totalBridgedOut` and enforces `bridgedOutSupply() > maxBridgedOutSupply` (`contracts/SyntheticToken.sol:275-284`). This is committed on the source chain once the LZ packet is sent — it is never rolled back if delivery fails.
- On the destination chain, `ProxyOFT._creditTo` calls `syntheticToken.mint(toAddress_, amount_)` (`contracts/ProxyOFT.sol:86-93`).
- `SyntheticToken._mint` increments `totalBridgedIn` and reverts `SurpassMaxBridgingSupply` when `bridgedInSupply() > maxBridgedInSupply` (`contracts/SyntheticToken.sol:338-344`).
- `bridgedInSupply() = totalBridgedIn - totalBridgedOut` (`contracts/SyntheticToken.sol:160-167`), so an attacker can push it to the cap by bridging in their own tokens to the victim's destination chain.
- When `_creditTo` reverts, `NonblockingLzAppUpgradeable` stores the payload in `failedMessages` instead of reverting the receive; completion depends entirely on `retryMessage` succeeding later (`contracts/dependencies/@layerzerolabs/solidity-examples/contracts-upgradeable/lzApp/NonblockingLzAppUpgradeable.sol`). The project docs acknowledge cap-based receive failure as a real revert reason requiring retry/unstuck procedures.

There is no failure path that credits the source chain back (no "quota off"): `totalBridgedOut` stays elevated and the victim holds nothing on the destination until a retry succeeds — which the attacker can keep failing.

### Impact Explanation
Temporary freezing of user funds: the victim's tokens are burned on the source chain (`totalBridgedOut` permanently incremented) while the destination mint is parked in `failedMessages`. As long as the attacker keeps `bridgedInSupply()` at `maxBridgedInSupply` by maintaining their bridged-in position, every `retryMessage` reverts on `SurpassMaxBridgingSupply`. The victim's funds are neither usable on the destination nor refundable on the source. Additionally, the elevated `totalBridgedOut` on the source chain consumes `maxBridgedOutSupply` headroom, degrading bridge liveness for all users of that synth.

### Likelihood Explanation
- Fully unprivileged: the attacker only needs to hold/bridge the synthetic token and call `ProxyOFT.sendFrom`, a public function with no role checks (`contracts/ProxyOFT.sol:115-128`).
- Requires `maxBridgedInSupply` to be set to a finite value on the destination deployment and enough attacker capital to fill `maxBridgedInSupply - bridgedInSupply()` — this is a capital cost, not a governance dependency; caps are a deployed configuration that the attacker exploits rather than sets.
- The attacker can time it: bridge in to saturate the cap before the victim's inbound message executes (cross-chain ordering is observable via LayerZero scan), causing the very first delivery to fail, then re-saturate whenever they bridge out before a retry lands.

### Recommendation
- In `ProxyOFT._creditTo`, do not let the cap check strand the message: either exempt OFT credit from `maxBridgedInSupply` (the cap should bound net outstanding bridge-in, and a retryable queue is not a clean enforcement point), or
- add a permissionless refund path: when a receive fails, allow the user to prove the failure and re-credit/burn-reverse on the source chain (i.e., decrement `totalBridgedOut`), mirroring the kernel fix's "turn quotas off on failure" semantics.
- At minimum, track failed-mint accounting so `bridgedOutSupply` is not permanently inflated by undelivered transfers.

### Proof of Concept
Foundry fork sketch (hardhat fixtures in `test/` already deploy `SyntheticToken`, `ProxyOFT`, `PoolRegistry`):

```solidity
// Setup: poolRegistry governor sets syntheticToken.updateMaxBridgedInSupply(CAP)
//         and trusted remotes between ProxyOFT instances on chain A (source) and B (dest).

// 1. Victim bridges V tokens A -> B
proxyOFT_A.sendFrom(victim, chainIdB, victim, V);   // _debitFrom: burn on A, totalBridgedOut_A += V

// 2. Attacker bridges enough synth to B so bridgedInSupply_B == CAP (front-running / cross-chain ordering)
proxyOFT_A2.sendFrom(attacker, chainIdB, attacker, CAP); // mint on B succeeds, fills cap

// 3. LZ delivers victim's packet on B: _creditTo -> _mint reverts SurpassMaxBridgingSupply
//    NonblockingLzApp stores failedMessages[srcA, proxyOFT_A, nonce]
assertEq(syntheticToken_B.balanceOf(victim), 0);
assertTrue(failedMessagesContains(nonce));

// 4. Anyone retries -> still reverts while cap saturated
vm.expectRevert(SurpassMaxBridgingSupply.selector);
nonblockingLzApp.retryMessage(chainIdA, abi.encodePacked(proxyOFT_A), nonce, payload);

// 5. Source chain accounting is permanently skewed:
assertEq(syntheticToken_A.totalBridgedOut(), V_before + V);  // never decremented
// victim's V is frozen until attacker releases cap headroom on B.
```