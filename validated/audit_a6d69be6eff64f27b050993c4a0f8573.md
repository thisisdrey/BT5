## Title
BatchValidateSign precompile leaves unbounded signature-recovery tasks running on the shared thread pool after a CPU timeout, allowing a malicious contract to starve block execution — (File: actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java)

### Summary
The `BatchValidateSign` TVM precompile submits ECDSA signature-recovery jobs to a small, statically-shared `ExecutorService` (`workers`) and waits on them with a CPU-time-bounded `CountDownLatch.await(...)`. If the wait times out, the precompile throws a "not enough time" exception and the calling transaction is aborted/reverted — but the already-submitted `RecoverAddrTask`s are never cancelled and continue to run to completion on the shared pool. Because the pool is reused by every transaction in the node process, an attacker who repeatedly triggers this timeout path can pile up uncancelled CPU-bound work on that fixed-size pool, starving subsequent (legitimate) `BatchValidateSign` calls from other transactions and slowing block application/production.

### Finding Description
`BatchValidateSign.doExecute` accepts up to `MAX_SIZE` (16) signatures per call and, for non-constant calls, dispatches one `RecoverAddrTask` (ECDSA public-key recovery) per signature to a shared executor sized `availableProcessors()/2 + 1`: [1](#0-0) 

The code awaits completion for only the caller's remaining CPU budget and, on timeout, throws without cancelling the outstanding futures: [2](#0-1) 

The submitted `RecoverAddrTask` has no cancellation/interruption hook — it always runs `recoverAddrBySign` to completion regardless of whether the parent transaction's CPU/energy budget has already been exhausted: [3](#0-2) 

This means the tokenized/metered CPU-time accounting done by `Program.checkCPUTimeLimit` and `MUtil.checkCPUTime()` elsewhere in the VM does not actually bound the real CPU work performed by this precompile: work already dispatched to the shared pool keeps consuming a core-bound worker thread after the "owning" transaction has been marked as timed out and had its energy spent. Since `workers` is a `static` field shared by all `BatchValidateSign` invocations across the whole node (all transactions, all blocks), a contract that reliably drives many calls into the timeout branch can keep the pool's limited threads (`availableProcessors()/2 + 1`) occupied with backlogged recovery jobs, delaying processing of `BatchValidateSign` calls made by other, unrelated transactions in the same or subsequent blocks — directly analogous to the CosmWasm advisory's "malicious contract can slow down block production" class, where CPU work triggered by a contract call is not properly bounded/cancelled relative to the metered execution.

### Impact Explanation
This is reachable by any unprivileged account: `BatchValidateSign` is a standard TVM precompile invocable via a plain `CALL`/`STATICCALL` from a smart contract, requiring no special permission. Repeatedly forcing timeouts (e.g., by crafting transactions whose energy/CPU limit is deliberately close to but below what's needed to let 16 concurrent ECDSA recoveries finish) causes CPU-bound work to accumulate on the small shared executor. Because the executor is sized relative to CPU cores and reused globally, sustained abuse degrades the throughput of subsequent transactions relying on the same precompile and consumes CPU resources across block boundaries, which can measurably slow block production/validation — a Medium-severity denial-of-service condition consistent with the referenced advisory's class, without requiring node crash, halt, or key compromise.

### Likelihood Explanation
Likelihood is moderate: constructing a transaction that reliably tips the CPU-time `await` into timeout while still queuing signature-recovery jobs requires some tuning (fee limit, energy limit, signature/word encoding, and the target CPU-time-left computation), but nothing about the mechanism is probabilistic or race-dependent for an attacker — it only depends on submitting a transaction whose deposited energy/time budget causes `getCPUTimeLeftInNanoSecond()` to expire before the recovery tasks finish, which is directly controllable by choosing `feeLimit`/energy limit and repeating the call. No SR/witness/peer privileges are needed.

### Recommendation
- Cancel/interrupt outstanding `Future`s (and pass an interruption-aware cancellation flag into `RecoverAddrTask`) when the `CountDownLatch.await` times out in `BatchValidateSign.doExecute`, so CPU work stops when the owning transaction stops being charged.
- Consider dedicating the executor's sizing/queueing policy so that a backlog from one client/transaction cannot block other unrelated calls indefinitely (e.g., bounded queue with rejection, or per-call thread budget rather than a shared global pool).
- Audit other precompiles/backgrounded thread pools (e.g., `VerifyTransferProof`'s `workersInConstantCall`/`workersInNonConstantCall`) for the same lack-of-cancellation pattern on timeout.

### Proof of Concept
1. Deploy a contract that calls the `batchvalidatesign(bytes32,bytes[],address[])` precompile with the maximum allowed 16 signatures.
2. Set the transaction's `feeLimit`/energy such that `getCPUTimeLeftInNanoSecond()` is small enough that the `CountDownLatch.await` (see `PrecompiledContracts.java:1201-1207`) times out before all 16 `RecoverAddrTask`s complete on the shared `workers` pool.
3. Repeat this transaction pattern rapidly from multiple accounts/contracts; each triggers the `notEnoughTime` throw path (`PrecompiledContracts.java:1204-1207`) but leaves its `RecoverAddrTask`s running uncancelled on the fixed-size shared executor (`PrecompiledContracts.java:1125-1135`, `1220-1236`).
4. Observe that the shared thread pool becomes backlogged with these uncancelled tasks, delaying `BatchValidateSign` calls made by unrelated, legitimate transactions and slowing overall transaction/block processing.

### Citations

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1123-1135)
```java
  public static class BatchValidateSign extends PrecompiledContract {

    private static final ExecutorService workers;
    private static final String workersName = "validate-sign-contract";
    private static final int ENGERYPERSIGN = 1500;
    private static final int MAX_SIZE = 16;
    private static final int ABI_HEADER_WORDS = 5;
    private static final int ABI_ITEM_WORDS = 6;

    static {
      workers = ExecutorServiceManager.newFixedThreadPool(workersName,
          Runtime.getRuntime().availableProcessors() / 2 + 1);
    }
```

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1191-1218)
```java
      } else {
        // add check
        CountDownLatch countDownLatch = new CountDownLatch(cnt);
        List<Future<RecoverAddrResult>> futures = new ArrayList<>(cnt);

        for (int i = 0; i < cnt; i++) {
          Future<RecoverAddrResult> future = workers
              .submit(new RecoverAddrTask(countDownLatch, hash, signatures[i], i));
          futures.add(future);
        }
        boolean withNoTimeout = countDownLatch
            .await(getCPUTimeLeftInNanoSecond(), TimeUnit.NANOSECONDS);

        if (!withNoTimeout) {
          logger.info("BatchValidateSign timeout");
          throw Program.Exception.notEnoughTime("call BatchValidateSign precompile method");
        }

        for (Future<RecoverAddrResult> future : futures) {
          RecoverAddrResult result = future.get();
          int index = result.nonce;
          if (DataWord.equalAddressByteArray(result.addr, addresses[index])) {
            res[index] = 1;
          }
        }
      }
      return Pair.of(true, res);
    }
```

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1220-1236)
```java
    @AllArgsConstructor
    private static class RecoverAddrTask implements Callable<RecoverAddrResult> {

      private CountDownLatch countDownLatch;
      private byte[] hash;
      private byte[] signature;
      private int nonce;

      @Override
      public RecoverAddrResult call() {
        try {
          return new RecoverAddrResult(recoverAddrBySign(this.signature, this.hash), nonce);
        } finally {
          countDownLatch.countDown();
        }
      }
    }
```
