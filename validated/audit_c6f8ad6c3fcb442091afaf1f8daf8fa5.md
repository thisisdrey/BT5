### Title
CPU-time budget for Sapling proof verification is silently unenforced due to unused `withNoTimeout` result, allowing unbounded thread-pool blocking - (File: actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java)

### Summary
`VerifyTransferProof.execute()` submits native zk-SNARK verification tasks (`SaplingCheckSpendTask`, `SaplingCheckOutputTask`, `SaplingCheckBingdingSig`) to shared, fixed-size thread pools (`workersInConstantCall` / `workersInNonConstantCall`) and awaits their completion with a deadline derived from the remaining CPU-time budget (`getCPUTimeLeftInNanoSecond()`). The boolean result of that timed wait, `withNoTimeout`, is computed but never checked. Execution unconditionally falls through to `future.get()` for every submitted future, which blocks indefinitely (no timeout) until the native call finishes, regardless of whether the CPU-time budget has already been exhausted.

### Finding Description [1](#0-0) 

```
boolean withNoTimeout = countDownLatch.await(getCPUTimeLeftInNanoSecond(),
    TimeUnit.NANOSECONDS);
boolean checkResult = true;
for (Future<Boolean> future : futures) {
  boolean eachTaskResult = future.get();
  checkResult = checkResult && eachTaskResult;
}
```

Compare this to the sibling precompile `BatchValidateSign`, which performs the exact same pattern but *does* act on the timeout result: [2](#0-1) 

```
boolean withNoTimeout = countDownLatch
    .await(getCPUTimeLeftInNanoSecond(), TimeUnit.NANOSECONDS);

if (!withNoTimeout) {
  logger.info("BatchValidateSign timeout");
  throw Program.Exception.notEnoughTime("call BatchValidateSign precompile method");
}
```

In `VerifyTransferProof`, `withNoTimeout` is assigned but never consulted — a dead variable. This is directly analogous to the reported bug class: the AIO error path in libkcapi allows completion callbacks to run and write into caller buffers *after* the caller has already decided to treat the operation as failed/errored (i.e., the "cancel"/error signal did not stop or bound the outstanding async work). Here, the CPU-time deadline is the intended "error/cancel" signal for the outstanding async proof-verification tasks, but the code ignores it and blocks on `future.get()` without any timeout, letting the native `librustzcash*` calls run to completion no matter how long they take. The per-opcode/per-call CPU budget that the rest of the TVM execution engine relies on to bound execution time is bypassed for this precompile.

### Impact Explanation
An unprivileged contract deployer/caller can invoke the `verifyTransferProof` precompile (address 0x66 range, reachable via any TVM `CALL`/`STATICCALL` with attacker-controlled Sapling proof-shaped input) in a way that keeps a native zk-proof verification thread running in the shared, fixed-size `workersInConstantCall`/`workersInNonConstantCall` pools well past the intended CPU-time cutoff. Because `future.get()` has no timeout, the calling VM thread — and therefore the block/transaction being processed — blocks until the native call returns, defeating the CPU-time enforcement that other precompiles (e.g. `BatchValidateSign`) correctly rely on to bound execution. Since these executor pools are static/shared across all `VerifyTransferProof` invocations, repeated malicious calls can exhaust the small (5-thread) pool, causing subsequent legitimate calls to queue and block as well, degrading or halting transaction/block processing (a reachable denial-of-service against block application in `Manager` via TVM execution). This maps to the "node halt" / "API the node can no longer serve" impact bucket.

### Likelihood Explanation
High: `verifyTransferProof` is a standard precompiled contract reachable from any smart contract via a single signed transaction; no special privileges are required. The unbounded `future.get()` call is unconditionally reached whenever the two-step latch await times out, which an attacker can reliably induce by crafting proof inputs that are slow for the native Sapling verifier (or simply by concurrently flooding calls to exhaust the fixed 5-thread pools so that legitimate work queues behind attacker work).

### Recommendation
Honor `withNoTimeout` the same way `BatchValidateSign` does: if `countDownLatch.await(...)` returns `false`, cancel all outstanding futures (`future.cancel(true)`) and throw `Program.Exception.notEnoughTime(...)` instead of falling through to unbounded `future.get()` calls. Additionally, consider using `future.get(timeout, unit)` per future (bounded by remaining CPU time) instead of the unbounded overload, and ensure cancelled/interrupted native calls are properly aborted so the worker thread is freed promptly rather than left running to completion.

### Proof of Concept
1. Deploy a contract that calls the `verifyTransferProof` precompile with a validly-sized (2080/2368/2464/2752-byte) but computation-heavy input (e.g., using the maximum spend/receive counts allowed, 2 spends + 2 receives, to maximize native verification work per call).
2. Set the calling transaction's `feeLimit`/energy such that `getCPUTimeLeftInNanoSecond()` is small relative to native verification time, or simply send many such calls concurrently so the 5-thread `workersInConstantCall`/`workersInNonConstantCall` pools saturate.
3. Observe that `countDownLatch.await` times out (`withNoTimeout == false`), yet the code proceeds to `future.get()` for each future and blocks the calling thread until the native zk verification actually completes — well beyond the CPU-time budget that should have aborted execution, unlike the equivalent check in `BatchValidateSign` which correctly throws `notEnoughTime`.

### Citations

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1201-1210)
```java
        boolean withNoTimeout = countDownLatch
            .await(getCPUTimeLeftInNanoSecond(), TimeUnit.NANOSECONDS);

        if (!withNoTimeout) {
          logger.info("BatchValidateSign timeout");
          throw Program.Exception.notEnoughTime("call BatchValidateSign precompile method");
        }

        for (Future<RecoverAddrResult> future : futures) {
          RecoverAddrResult result = future.get();
```

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1589-1595)
```java
        boolean withNoTimeout = countDownLatch.await(getCPUTimeLeftInNanoSecond(),
            TimeUnit.NANOSECONDS);
        boolean checkResult = true;
        for (Future<Boolean> future : futures) {
          boolean eachTaskResult = future.get();
          checkResult = checkResult && eachTaskResult;
        }
```
