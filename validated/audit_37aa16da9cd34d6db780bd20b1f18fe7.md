## Analysis

The reported Prysm bug is a class of "blocking/timeout-bypass concurrency" bug: a verifier goroutine's timeout path doesn't actually stop pending work from later blocking a thread/goroutine indefinitely. The equivalent bug class exists in java-tron's TVM precompiled-contract signature/zk-proof batch verifiers.

`BatchValidateSign.doExecute()` in `PrecompiledContracts.java` correctly checks the `CountDownLatch.await()` result and aborts with `notEnoughTime` if it times out: [1](#0-0) 

`VerifyTransferProof.execute()` implements the identical pattern but **omits the timeout check**, unconditionally calling the blocking, non-timed `future.get()` for every submitted task regardless of whether the latch timed out: [2](#0-1) 

### Title
Unbounded blocking on `Future.get()` after CPU-time budget exhausted in `VerifyTransferProof` precompile can stall block processing - (File: `actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java`)

### Summary
`VerifyTransferProof.execute()` (TVM precompile address `...0001000002`, reachable by any smart-contract `CALL`) submits Sapling spend/output/binding-signature verification tasks to one of two shared, statically-sized 5-thread pools (`workersInConstantCall` / `workersInNonConstantCall`) and waits on a `CountDownLatch` bounded by the transaction's remaining CPU-time budget via `getCPUTimeLeftInNanoSecond()`. The boolean result of `countDownLatch.await(...)` is stored in `withNoTimeout` but **never checked**. Execution falls through unconditionally into a loop that calls `future.get()` (no timeout) on every submitted task, blocking the calling thread until each native zk-SNARK check completes — with no bound on how long that can take.

### Finding Description
Every other precompile that uses this CountDownLatch/CPU-budget pattern (`BatchValidateSign`) explicitly aborts with `Program.Exception.notEnoughTime(...)` when the latch times out, per [1](#0-0) . `VerifyTransferProof` instead computes `withNoTimeout` and discards it, immediately calling `future.get()` on each `Future<Boolean>` regardless of the latch outcome, per [3](#0-2) . Because `workersInConstantCall`/`workersInNonConstantCall` are fixed-size pools of only 5 threads shared across **all** shielded-transfer verifications processed by the node [4](#0-3) , an attacker who repeatedly broadcasts `TriggerSmartContract` transactions performing `CALL`s to this precompile with the maximum allowed spend/receive counts (2 spends + 2 outputs + 1 binding-sig task = 5 tasks per call, matching pool size exactly) can keep the pool continuously saturated with expensive native `librustzcash` verification work. Once the CPU-time budget for a given call is exhausted, the code does not abort — it blocks on `future.get()` waiting for queued tasks behind other concurrent malicious calls to finish, with no timeout at all.

### Impact Explanation
Since TVM precompile execution occurs synchronously inside transaction/block processing (`Program.callToPrecompiledAddress` → `contract.execute(data)`, see [5](#0-4) ), an unbounded block inside this precompile stalls the thread applying the block/transaction in `Manager`. A sustained flood of such transactions can keep the shared 5-thread pool backlogged indefinitely, causing transaction/block validation to hang well past the node's intended CPU-time limits — a node halt / liveness DoS reachable from an unprivileged, anonymous transaction broadcaster (no special permission needed, only requires shielded TRC-20/zk-SNARK support enabled).

### Likelihood Explanation
The path is reachable by any account that can broadcast a `TriggerSmartContract` transaction performing a `CALL` (or has it invoked via a deployed contract) to the `VerifyTransferProof` precompile address with a `data` blob of the expected size and near-maximal spend/receive counts (bounds only enforced at 1–2 each). No signature threshold, permission, or special account status is required — it is a pure TVM opcode/precompile call, matching the allowed-scope categories (TVM opcodes/precompiles, actuator execute paths). The only requirement is that shielded/zk-SNARK precompile support is active on the network.

### Recommendation
Check the `withNoTimeout` result from `countDownLatch.await(...)` in `VerifyTransferProof.execute()` exactly as `BatchValidateSign` does, and throw `Program.Exception.notEnoughTime(...)` (optionally cancelling in-flight futures) when it is `false`, instead of falling through to unbounded `future.get()` calls.

### Proof of Concept
1. Deploy/trigger a contract that repeatedly issues `CALL`s to the `VerifyTransferProof` precompile address (`0000...0001000002`) with well-formed but maximal shielded-transfer payloads (`spendCount=2`, `receiveCount=2`), submitted concurrently from many accounts/threads (non-constant calls use `workersInNonConstantCall`, a 5-thread pool).
2. Because each call submits up to 5 tasks and the pool only has 5 threads, concurrent malicious calls saturate the pool with genuine (slow) native zk-SNARK verification work.
3. For calls whose CPU-time budget expires while queued tasks are still pending, `countDownLatch.await()` returns `false`, but execution proceeds directly into the `future.get()` loop at [6](#0-5) , blocking that transaction-processing thread with no timeout until the backlog drains.
4. Repeating this with a steady stream of transactions keeps the shared pool permanently saturated, causing transaction/block application to stall well beyond configured CPU-time limits, producing a sustained processing delay/halt.

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

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1449-1457)
```java
    private static final ExecutorService workersInConstantCall;
    private static final ExecutorService workersInNonConstantCall;
    private static final String constantCallName = "verify-transfer-constant-call";
    private static final String nonConstantCallName = "verify-transfer-non-constant-call";

    static {
      workersInConstantCall = ExecutorServiceManager.newFixedThreadPool(constantCallName, 5);
      workersInNonConstantCall = ExecutorServiceManager.newFixedThreadPool(nonConstantCallName, 5);
    }
```

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1559-1596)
```java
        int threadCount = spendCount + receiveCount + 1;
        CountDownLatch countDownLatch = new CountDownLatch(threadCount);
        List<Future<Boolean>> futures = new ArrayList<>(threadCount);
        ExecutorService workers;
        if (isConstantCall()) {
          workers = workersInConstantCall;
        } else {
          workers = workersInNonConstantCall;
        }

        // submit check spend task
        for (int i = 0; i < spendCount; i++) {
          Future<Boolean> futureCheckSpend = workers
              .submit(new SaplingCheckSpendTask(countDownLatch, spendCv[i], anchor[i],
                  nullifier[i], rk[i], spendProof[i], spendAuthSig[i], signHash));
          futures.add(futureCheckSpend);
        }
        //submit check output task
        for (int i = 0; i < receiveCount; i++) {
          Future<Boolean> futureCheckOutput = workers
              .submit(new SaplingCheckOutputTask(countDownLatch, receiveCv[i], receiveCm[i],
                  receiveEpk[i], receiveProof[i]));
          futures.add(futureCheckOutput);
        }
        // submit check binding signature
        Future<Boolean> futureCheckBindingSig = workers
            .submit(new SaplingCheckBingdingSig(countDownLatch, value, bindingSig,
                signHash, spendCvs, spendCount * 32, receiveCvs, receiveCount * 32));
        futures.add(futureCheckBindingSig);

        boolean withNoTimeout = countDownLatch.await(getCPUTimeLeftInNanoSecond(),
            TimeUnit.NANOSECONDS);
        boolean checkResult = true;
        for (Future<Boolean> future : futures) {
          boolean eachTaskResult = future.get();
          checkResult = checkResult && eachTaskResult;
        }
        if (checkResult) {
```

**File:** actuator/src/main/java/org/tron/core/vm/program/Program.java (L1750-1753)
```java
      contract.setConstantCall(isConstantCall());
      contract.setVmShouldEndInUs(getVmShouldEndInUs());
      Pair<Boolean, byte[]> out = contract.execute(data);

```
