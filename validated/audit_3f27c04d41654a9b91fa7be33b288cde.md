### Title
Transient `MSG_SENDER` leaks the user's identity to every contract touched during `Operator.execute`, letting any callee impersonate the user and withdraw their collateral - ([File: contracts/utils/SynthContext.sol](contracts/utils/SynthContext.sol))

### Summary
`Operator.execute` stores the initiating EOA in transient slot `MSG_SENDER_STORAGE` for the duration of the whole batch. `SynthContext._msgSender()` resolves to that stored EOA whenever `msg.sender == operator`, with no check on *which* call inside the batch is currently executing. Any contract reached during the batch — including attacker-deployed tokens, fake pools, or malicious routers invoked as calldata targets or via token callbacks — therefore executes privileged, identity-bearing functions (`DepositToken.withdraw`, `transfer`, `approve`, `DebtToken.repay`, `SyntheticToken.transfer`, etc.) *as the victim*. This mirrors CVE-2023-29541's class: a benign-looking artifact (a downloaded `.desktop` file / an arbitrary call inside a multicall) is interpreted inside a trusted context and runs attacker-controlled actions with the user's authority.

### Finding Description
`Operator.execute` writes `tstore(MSG_SENDER_STORAGE, msg.sender)` once at batch start and clears it at the end (`contracts/Operator.sol:20-24`). `SynthContext._msgSender()` then returns that EOA for **any** call whose immediate `msg.sender` is the Operator (`contracts/utils/SynthContext.sol:14-24`). There is no binding between the transient sender and the `Call.target` the user actually intended.

Concretely, `DepositToken.withdraw(amount_, to_)` (`contracts/DepositToken.sol:406-412`):
- resolves `_msgSender()` → the victim EOA while inside the batch,
- checks only `_revertIfLocked(victim, amount_)` (`unlockedBalanceOf` — no authorization),
- calls `_withdraw(account_ = victim, to_ = attacker)`, which burns the victim's `msdToken` and calls `treasury.pull(attacker, _withdrawn)` (`contracts/DepositToken.sol:536-554`).

Attack path:
1. Victim EOA calls `operator.execute([...])` where one call targets (or transitively reaches) an attacker contract — e.g., batching a swap that routes through an attacker-deployed token whose `transfer`/`transferFrom` hook executes the payload, or a malicious zap/router the user was induced to include (the on-chain analog of "downloading the .desktop file").
2. During that callback, `msg.sender == Operator` for any call the malicious contract makes into a `SynthContext` contract.
3. The malicious contract calls `msdWETH.withdraw(aliceUnlockedBalance, attacker)` and receives the underlying collateral from `Treasury.pull`.

None of the guards stop it: `withdraw` has no `onlyPool`/`onlyIfSmartFarmingManager` restriction, `nonReentrant` on `DepositToken` is a fresh entry (not a reentry), the victim's unlocked balance check is the only limit, and `whenNotShutdown`/pause flags are off in normal operation. Identity-bearing surface also includes `DepositToken.transfer/approve`, `SyntheticToken.transfer`, `DebtToken.repay`, `VesperGateway.withdraw`, `NativeTokenGateway.withdraw`, and `SmartFarmingManager.flashRepay` — all keyed on `_msgSender()`.

### Impact Explanation
Direct theft of user funds: any collateral that is not locked against debt (users with zero or low LTV positions — i.e., most depositors) can be fully withdrawn to the attacker in the same transaction as a legitimate batch, minus the withdraw fee. The attacker can also drain `msdToken`/`msAsset` balances via `transfer`, or repay/manipulate the victim's debt position.

### Likelihood Explanation
Requires the victim to execute an `Operator.execute` batch that touches attacker-controlled code (a scam token in a swap path, a malicious zap contract, a phishing-crafted batch). This is a realistic interaction pattern for a multicall/operator design and requires no privileged role, no oracle manipulation, and no governance action. Unprivileged attacker with a deployed contract only.

### Recommendation
Bind the forwarded identity to the current call context. Options:
- Have `Operator` pass the actual sender via calldata (ERC-2771-style appended sender) instead of ambient transient storage, so only the intended `target` contract can consume it.
- Or store `MSG_SENDER` per-target: `tstore(keccak(target), msg.sender)` inside the loop and clear it before the next call, and have `SynthContext` read `keccak(address(this))`-keyed slots, so only the contract the user explicitly called sees their identity.
- Short-term mitigation: document that `Operator.execute` must never be used to call untrusted contracts, and warn integrators/routers.

### Proof of Concept
Hardhat fork sketch (deployed config on mainnet/optimism; `Operator` is live at `0xc06D63…D360` on mainnet):

```solidity
// Attacker-deployed token included in a swap batch, or a malicious router target.
contract EvilToken {
    function transferFrom(address, address, uint256) external returns (bool) {
        // msg.sender == Operator here; getActualMsgSender() == victim EOA
        uint256 unlocked = IDepositToken(MSD_WETH).unlockedBalanceOf(VICTIM);
        IDepositToken(MSD_WETH).withdraw(unlocked, ATTACKER); // burns victim's msdWETH, Treasury pulls WETH to ATTACKER
        return true;
    }
}

// Victim tx (EOA):
// operator.execute([Call({target: swapRouter, value: 0, callData: swap(...)})])
// where the swap path pulls EvilToken -> transferFrom executes the payload.

// Assert after tx:
//   WETH.balanceOf(attacker) == unlocked minus withdrawFee
//   msdWETH.balanceOf(victim) == locked portion only
```

Reproduce on an Optimism/Base fork: deposit WETH via `msdWETH.deposit` for a victim with no debt, then run `operator.execute` whose batch reaches the malicious contract; the treasury transfers the victim's WETH to the attacker in the same transaction.

One caveat to flag: exploitation requires the victim's batch to reach attacker-controlled code (phishing/malicious token), analogous to the CVE's required user interaction; users whose entire balance is locked by debt are not exposed to `withdraw`, though `transfer`-based draining is bounded the same way.