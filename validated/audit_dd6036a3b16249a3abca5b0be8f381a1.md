### Title
Sapling `VerifyTransferProof` precompile ignores CPU-time-budget timeout, letting the interpreter thread block past its allotted execution time - (File: `actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java`)

### Summary
The `VerifyTransferProof` precompiled contract (Sapling shielded-transfer proof verification) submits per-spend/per-output/binding-signature verification jobs to a shared thread pool and waits on a `CountDownLatch` bounded by the transaction's remaining CPU time budget. The `boolean` result of that bounded wait (`withNoTimeout`) is computed but never checked before the code unconditionally calls `future.get()` on every submitted task, which blocks until each native (`librustzcash`) verification actually completes. This defeats the time-budget enforcement that the sibling precompile `BatchValidateSign` correctly implements in the very same class.

### Finding Description
In `VerifyTransferProof.execute` (Sapling `zksnark::verify_transfer_proof` precompile), the code does: [1](#0-0) 

```
boolean withNoTimeout = countDownLatch.await(getCPUTimeLeftInNanoSecond(),
    TimeUnit.NANOSECONDS);
boolean checkResult = true;
for (Future<Boolean> future : futures) {
  boolean eachTaskResult = future.get();
  checkResult = checkResult && eachTaskResult;
}
```

`withNoTimeout` — the outcome of waiting only up to the remaining CPU-time budget (`getCPUTimeLeftInNanoSecond()`) — is never inspected. Regardless of whether the latch actually timed out, execution proceeds straight into the `future.get()` loop, which blocks the calling (interpreter) thread until each background zk-proof verification task (`SaplingCheckSpendTask`, `SaplingCheckOutputTask`, `SaplingCheckBingdingSig`) finishes in the shared `workersInConstantCall`/`workersInNonConstantCall` pool.

This is the direct analog of the reported bug class: an asynchronous operation has a bounded-wait/timeout mechanism, but the timeout signal is not handled, so the caller proceeds as if the operation completed normally and ends up depending on state/results from an operation that was supposed to have been abandoned when the timeout fired.

By contrast, the sibling method `BatchValidateSign` in the same file correctly checks the identical pattern and aborts with an `OutOfTimeException`: [2](#0-1) 

```
boolean withNoTimeout = countDownLatch
    .await(getCPUTimeLeftInNanoSecond(), TimeUnit.NANOSECONDS);

if (!withNoTimeout) {
  logger.info("BatchValidateSign timeout");
  throw Program.Exception.notEnoughTime("call BatchValidateSign precompile method");
}
```

`VerifyTransferProof` lacks this guard entirely, so the energy/CPU metering contract that the TVM relies on (each opcode/precompile must complete, throw, or be capped within its remaining CPU budget — enforced elsewhere via `MUtil.checkCPUTime()`/`OutOfTimeException` in `actuator/src/main/java/org/tron/core/vm/utils/MUtil.java`) is bypassed for this specific precompile.

### Impact Explanation
Any account can invoke the Sapling shielded transfer precompile (`zksnark::verify_transfer_proof`, address `0x0e`) from a contract call, so this is reachable by an unprivileged contract deployer/caller. Because the timeout is silently ignored, a caller can craft or trigger conditions (e.g., heavy concurrent load on the fixed 5-thread `workersInConstantCall`/`workersInNonConstantCall` pools from many concurrent shielded-proof calls) that make individual native `librustzcash` verification tasks queue up and run past the transaction's allotted CPU time. Instead of the transaction failing fast with an `OutOfTimeException` (as `BatchValidateSign` does), the interpreter thread executing block validation blocks indefinitely in `future.get()`, holding up progress of the transaction/block-execution loop in `Manager`/`VMActuator`. This can be leveraged to stall block production/validation (denial of service to the whole node's transaction-processing pipeline) since a single stuck precompile call blocks the single-threaded execution of the containing transaction and, by extension, the block-application sequence.

### Likelihood Explanation
Triggering the code path only requires submitting a transaction that calls the Sapling `verifyTransferProof` precompile with valid-shaped (not necessarily cryptographically valid) input — a normal, permissionless operation. Because the shared thread pools (`workersInConstantCall`, `workersInNonConstantCall`) are fixed-size (5 threads) and shared across all concurrent invocations of this precompile network-wide within a node, an attacker can flood the pool with many simultaneous shielded-transfer proof calls to make individual tasks queue past the CPU budget, deterministically hitting the unhandled-timeout path.

### Recommendation
Check `withNoTimeout` immediately after the `countDownLatch.await(...)` call in `VerifyTransferProof.execute` (mirroring `BatchValidateSign`) and, if `false`, abort with `Program.Exception.notEnoughTime(...)` (or an equivalent `OutOfTimeException`) instead of proceeding to call `future.get()` on the outstanding futures. Additionally consider cancelling/interrupting the outstanding `Future`s on timeout so the shared worker pool is not left processing abandoned work.

### Proof of Concept
Not independently executable from the index alone (requires running a TVM node and issuing concurrent contract calls to the Sapling `verifyTransferProof` precompile to saturate the 5-thread pool and force `getCPUTimeLeftInNanoSecond()` to elapse before all `SaplingCheckSpendTask`/`SaplingCheckOutputTask`/`SaplingCheckBingdingSig` futures complete). The code-level root cause — computing `withNoTimeout` but never branching on it before the blocking `future.get()` loop — is directly shown in the cited lines (`actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java:1589-1600`) and is confirmed by contrast with the correctly-guarded identical pattern in `BatchValidateSign` (`actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java:1201-1207`) in the same file.

### Citations

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1201-1207)
```java
        boolean withNoTimeout = countDownLatch
            .await(getCPUTimeLeftInNanoSecond(), TimeUnit.NANOSECONDS);

        if (!withNoTimeout) {
          logger.info("BatchValidateSign timeout");
          throw Program.Exception.notEnoughTime("call BatchValidateSign precompile method");
        }
```

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1589-1600)
```java
        boolean withNoTimeout = countDownLatch.await(getCPUTimeLeftInNanoSecond(),
            TimeUnit.NANOSECONDS);
        boolean checkResult = true;
        for (Future<Boolean> future : futures) {
          boolean eachTaskResult = future.get();
          checkResult = checkResult && eachTaskResult;
        }
        if (checkResult) {
          return insertLeaves(frontier, leafCount, receiveCm);
        } else {
          return Pair.of(true, DataWord.ZERO().getData());
        }
```
