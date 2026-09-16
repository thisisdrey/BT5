### Title
Missing CPU-time timeout enforcement in the Sapling `VerifyTransferProof` precompile causes hardware/timing-dependent, non-deterministic transaction execution - (File: `actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java`)

### Summary
The Sapling shielded-transfer proof verification precompile (`VerifyTransferProof`) launches multi-threaded zk-SNARK verification tasks and computes a CPU-time-budget timeout flag (`withNoTimeout`), but never checks it before blocking indefinitely on `Future.get()`. This is the exact bug-class described in the CKB advisory (GHSA-q73f-w3h7-7wcc): a syscall/precompile whose result and execution time can silently diverge from the protocol's deterministic accounting rules, making transaction validation outcome depend on wall-clock timing, machine speed, and thread-pool contention rather than purely on the transaction's inputs.

### Finding Description
`VerifyTransferProof.execute()` submits parallel spend/output/binding-signature verification tasks to a shared `ExecutorService`, and computes the CPU-time deadline via: [1](#0-0) 

Unlike the sibling precompile `BatchValidateSign`, which correctly aborts with an `OutOfTimeException` when the CPU budget is exceeded: [2](#0-1) 

`VerifyTransferProof` computes the identical `withNoTimeout` boolean but discards it and unconditionally proceeds to call `future.get()` on every submitted task: [3](#0-2) 

`Future.get()` with no timeout argument blocks until the native `librustzcash` computation finishes, regardless of whether the transaction's allotted CPU-time slice (`vmShouldEndInUs`) has already been exhausted. The CPU-time budget that this deadline represents is itself asymmetric by design: block-producing witnesses execute with `cpuLimitRatio = 1.0` while verifying nodes replay with a reduced `getMinTimeRatio()`/`getMaxTimeRatio()` factor: [4](#0-3) 

Because `VerifyTransferProof` ignores its own timeout signal, whether/when a "CPU timeout" (`Program.Exception.notEnoughTime`) is ultimately raised for the same transaction now depends on real wall-clock conditions of the thread pool (fixed 5-thread pool shared across all Sapling transfer transactions, native library call latency, GC pauses, machine speed) instead of being derived deterministically from the protocol's accounting rules — precisely the pattern of "syscall whose result/side-effects are computed under wall-clock/scheduling conditions that differ node to node," which the CKB `load_cell_data_hash` advisory flags as nondeterministic verification.

### Impact Explanation
If the node that produces a block (100% time ratio, presumably faster/idle) manages to complete `VerifyTransferProof` and successfully commits the shielded transfer, while a verifying node — replaying with a reduced time ratio, under load, or contending for the same small fixed thread pool with concurrently processed Sapling transactions — takes long enough that the equivalent VM-level `checkCPUTimeLimit` call fires a different outcome (`OUT_OF_TIME` contract result vs. success), the two nodes disagree on the transaction's `contractResult` and the resulting block/state root, causing a **chain split**. Because Sapling proof verification is expensive native cryptographic work, this can also be triggered/aggravated deliberately by submitting many shielded transfer transactions to saturate the shared 5-thread executor, worsening timing divergence between nodes with different hardware or load — an unprivileged, single-signed-transaction-reachable path (any account can submit a shielded transfer contract call).

### Likelihood Explanation
`VerifyTransferProof` is reachable directly by any account broadcasting a `ShieldedTransferContract`/related TVM call that invokes this precompile address, with no special privilege required. The missing check is a straightforward code-comparison bug (present in the sibling `BatchValidateSign` implementation but absent here), so it is easy to trigger reliably by crafting valid-size shielded transfer proof data and relying on normal network heterogeneity (different validator hardware/load) to produce divergent timing outcomes; an attacker can also actively induce divergence by flooding the small shared thread pool.

### Recommendation
In `VerifyTransferProof.execute()`, check the `withNoTimeout` result exactly as `BatchValidateSign` does, and throw `Program.Exception.notEnoughTime(...)` (or cancel the outstanding futures) when the CPU-time budget is exceeded, before calling `future.get()`. This restores deterministic, protocol-defined CPU-time accounting for this precompile instead of relying on unconditional indefinite blocking.

### Proof of Concept
1. Submit a valid `TriggerSmartContract`/Sapling `ShieldedTransferContract` transaction whose call data reaches `VerifyTransferProof.execute()` with a spend/receive count combination (2 spends + 2 receives) that maximizes native proof-verification work.
2. On a slow/loaded validator (or one contending for the fixed 5-thread `workersInNonConstantCall` pool via concurrently broadcast shielded transactions), the wall-clock time to complete all `Future.get()` calls exceeds `vmShouldEndInUs`.
3. Because `withNoTimeout` is never checked in `execute()`, the precompile does not itself throw a timeout, and the surrounding VM only detects staleness at the next `checkCPUTimeLimit` opcode boundary check — meaning whether the transaction ends up `SUCCESS` or `OUT_OF_TIME` can now differ from the block-producing node (running with `cpuLimitRatio = 1.0`) versus a verifying node running with `getMinTimeRatio()`, or between two verifying nodes with different available thread-pool capacity/hardware — since `BatchValidateSign`'s analogous code path (line 1204-1207) would instead deterministically raise the same exception at the same accounted CPU-time point regardless of real elapsed time relative to the enforcement path being bypassed.

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

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1589-1596)
```java
        boolean withNoTimeout = countDownLatch.await(getCPUTimeLeftInNanoSecond(),
            TimeUnit.NANOSECONDS);
        boolean checkResult = true;
        for (Future<Boolean> future : futures) {
          boolean eachTaskResult = future.get();
          checkResult = checkResult && eachTaskResult;
        }
        if (checkResult) {
```

**File:** actuator/src/main/java/org/tron/core/actuator/VMActuator.java (L698-721)
```java
  private double getCpuLimitInUsRatio() {

    double cpuLimitRatio;

    if (ExecutorType.ET_NORMAL_TYPE == executorType) {
      // self witness generates block
      if (blockCap != null && blockCap.generatedByMyself
          && !blockCap.hasWitnessSignature()) {
        cpuLimitRatio = 1.0;
      } else {
        // self witness or other witness or fullnode verifies block
        if (trx.getRet(0).getContractRet() == contractResult.OUT_OF_TIME) {
          cpuLimitRatio = CommonParameter.getInstance().getMinTimeRatio();
        } else {
          cpuLimitRatio = CommonParameter.getInstance().getMaxTimeRatio();
        }
      }
    } else {
      // self witness or other witness or fullnode receives tx
      cpuLimitRatio = 1.0;
    }

    return cpuLimitRatio;
  }
```
