The overflow is real and exploitable. Let me trace it precisely.

**`_afterTimelock` (line 297-299):**
```solidity
function _afterTimelock(address pool_) private view returns (uint32) {
    return uint32(block.timestamp + oracleStopLossConfig[pool_].timelock);
}
```

The addition is done in `uint256` space (no revert), then **explicitly truncated** to `uint32` — Solidity 0.8's overflow protection does not apply to explicit casts.

**`_requireElapsed` (line 301-303):**
```solidity
function _requireElapsed(uint32 executeAfter) private view {
    if (block.timestamp < executeAfter) revert ...;
}
```

**Concrete overflow math (July 2026, `block.timestamp ≈ 1_753_000_000`):**

| Step | Value |
|---|---|
| `block.timestamp` | `1_753_000_000` |
| `type(uint32).max` | `4_294_967_295` |
| Sum (uint256) | `6_047_967_295` |
| `uint32(sum)` | `6_047_967_295 mod 4_294_967_296 = 1_752_999_999` |

`1_752_999_999 < 1_753_000_000` → `executeAfter` is **one second in the past** → `_requireElapsed` passes immediately.

**Attack path:**

1. Pool initialized with `timelock = 0` (legitimate starting config).
2. Pool admin calls `proposeOracleStopLossTimelock(pool, type(uint32).max)`.
   - `_afterTimelock` → `uint32(ts + 0)` = current `ts` → `pendingTimelockExecuteAfter = ts`.
3. Pool admin calls `executeOracleStopLossTimelock(pool)` in the **same block**.
   - `_requireElapsed(ts)`: `ts < ts` → false → passes. `timelock` is now `type(uint32).max`.
4. Pool admin calls `proposeOracleStopLossDrawdown(pool, 0)`.
   - `_afterTimelock` → `uint32(ts + type(uint32).max)` = `ts - 1` (past timestamp).
   - `pendingDrawdownExecuteAfter = ts - 1`.
5. Pool admin calls `executeOracleStopLossDrawdown(pool)` in the **same block**.
   - `_requireElapsed(ts - 1)`: `ts < ts - 1` → false → passes. `drawdownE6 = 0`.

Stop-loss is now permanently disabled. The same overflow applies to `proposeOracleStopLossDecay` and `proposeOracleStopLossHighWatermarks` — all timelocks are bypassed.

**Scope gate check:**

The "Admin-boundary break" allowed impact explicitly includes *"pool admin bypasses timelocks"*. The timelock exists specifically to give LPs a reaction window against pool admin parameter changes (per the contract's own NatSpec: *"timelocked so LPs can react"*). The pool admin is not the factory owner or oracle admin — it is the semi-trusted role the timelock is designed to constrain. No malicious factory deployment or off-chain oracle data is needed.

---

### Title
uint32 Overflow in `_afterTimelock` Allows Pool Admin to Permanently Bypass All Stop-Loss Timelocks in a Single Block — (`metric-periphery/contracts/extensions/OracleValueStopLossExtension.sol`)

### Summary
`_afterTimelock` truncates `block.timestamp + timelock` to `uint32`. When `timelock = type(uint32).max`, the sum overflows the `uint32` range and wraps to a past timestamp, making every subsequent proposal immediately executable and permanently defeating the timelock invariant.

### Finding Description
`_afterTimelock` computes the unlock timestamp as:

```solidity
return uint32(block.timestamp + oracleStopLossConfig[pool_].timelock);
``` [1](#0-0) 

The addition is performed in `uint256` (no revert), then explicitly cast to `uint32`. Solidity 0.8 checked arithmetic does not protect explicit narrowing casts. With `timelock = type(uint32).max` and `block.timestamp ≈ 1.75 × 10⁹`, the sum `≈ 6.05 × 10⁹` exceeds `type(uint32).max = 4.29 × 10⁹`, wrapping to a value approximately one second **before** `block.timestamp`.

`_requireElapsed` then checks:

```solidity
if (block.timestamp < executeAfter) revert ...;
``` [2](#0-1) 

Since `executeAfter` is now a past timestamp, the check passes immediately for every proposal — drawdown, decay, and high-watermark — made while `timelock = type(uint32).max` is active.

There is no validation on `newTimelock` in `proposeOracleStopLossTimelock`:

```solidity
function proposeOracleStopLossTimelock(address pool_, uint32 newTimelock) external onlyPoolAdmin(pool_) {
``` [3](#0-2) 

### Impact Explanation
The timelock is the sole mechanism protecting LPs from immediate pool-admin parameter changes. The contract's own NatSpec states: *"Drawdown and decay changes are timelocked so LPs can react."* Once the pool admin sets `timelock = type(uint32).max`, they can set `drawdownE6 = 0` (disabling stop-loss entirely), raise decay to maximum, or reset watermarks to zero — all in the same block, with no LP reaction window. Swaps that would have been reverted by the stop-loss check proceed unchecked, exposing LP principal to undetected value drain.

### Likelihood Explanation
The pool admin role is a single EOA or multisig per pool. Any pool initialized with `timelock = 0` (a valid and common starting configuration) is immediately exploitable in two transactions within one block. Pools with a non-zero initial timelock require one wait period before the exploit, after which all future timelocks are permanently bypassed.

### Recommendation
Replace the truncating cast with a checked addition and cap `newTimelock` to a safe maximum:

```solidity
// In proposeOracleStopLossTimelock, validate before storing:
uint256 MAX_TIMELOCK = 365 days; // or type(uint32).max / 2
if (newTimelock > MAX_TIMELOCK) revert TimelockTooLarge(newTimelock);

// In _afterTimelock, use safe arithmetic:
uint256 result = block.timestamp + oracleStopLossConfig[pool_].timelock;
if (result > type(uint32).max) revert TimelockOverflow();
return uint32(result);
```

### Proof of Concept
```solidity
// Foundry integration test sketch
function test_uint32_overflow_bypasses_timelock() public {
    // 1. Initialize pool with timelock = 0
    bytes memory initData = abi.encode(uint32(500_000), uint32(58), uint32(0));
    // ... pool creation with extension ...

    // 2. Propose timelock = type(uint32).max (no wait needed, current timelock = 0)
    vm.prank(poolAdmin);
    ext.proposeOracleStopLossTimelock(pool, type(uint32).max);

    // 3. Execute immediately in same block (pendingTimelockExecuteAfter == block.timestamp)
    vm.prank(poolAdmin);
    ext.executeOracleStopLossTimelock(pool);
    // timelock is now type(uint32).max

    // 4. Propose drawdown = 0 (disables stop-loss)
    vm.prank(poolAdmin);
    ext.proposeOracleStopLossDrawdown(pool, 0);
    // _afterTimelock wraps: uint32(ts + type(uint32).max) = ts - 1 (past)

    // 5. Execute immediately — no wait
    vm.prank(poolAdmin);
    ext.executeOracleStopLossDrawdown(pool);

    // 6. Assert stop-loss is disabled
    assertEq(ext.oracleStopLossConfig(pool).drawdownE6, 0);

    // 7. Perform a swap with real drawdown — assert no revert
    // ... swap call that would have triggered OracleStopLossTriggered ...
}
```

### Citations

**File:** metric-periphery/contracts/extensions/OracleValueStopLossExtension.sol (L78-84)
```text
  function proposeOracleStopLossTimelock(address pool_, uint32 newTimelock) external onlyPoolAdmin(pool_) {
    PoolStopLossSchedule storage sched = _initializedSchedule(pool_);
    uint32 executeAfter = _afterTimelock(pool_);
    sched.pendingTimelock = newTimelock;
    sched.pendingTimelockExecuteAfter = executeAfter;
    emit OracleStopLossTimelockProposed(pool_, newTimelock, executeAfter);
  }
```

**File:** metric-periphery/contracts/extensions/OracleValueStopLossExtension.sol (L297-299)
```text
  function _afterTimelock(address pool_) private view returns (uint32) {
    return uint32(block.timestamp + oracleStopLossConfig[pool_].timelock);
  }
```

**File:** metric-periphery/contracts/extensions/OracleValueStopLossExtension.sol (L301-303)
```text
  function _requireElapsed(uint32 executeAfter) private view {
    if (block.timestamp < executeAfter) revert OracleStopLossTimelockNotElapsed(executeAfter, block.timestamp);
  }
```
