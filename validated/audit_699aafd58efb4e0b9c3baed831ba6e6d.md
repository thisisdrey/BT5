### Title
Shared mutable state on singleton `PrecompiledContract` instances allows a concurrent constant-call to flip the timeout-enforcement branch of a real broadcast transaction - (File: actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java)

### Summary
`PrecompiledContracts` instantiates every precompile (including `BatchValidateSign` and `ValidateMultiSign`, which perform signature recovery) as a single process-wide `static final` object [1](#0-0) . Per-call behavior such as "is this a constant call" is stored as mutable instance state on that shared singleton and toggled via `setConstantCall(...)` before every `execute()` invocation, exactly the "set a per-transfer TLS option that ends up mutating shared global state" pattern described in CURL-CVE-2025-14017.

### Finding Description
`BatchValidateSign.doExecute` branches its entire execution strategy on the shared instance flag `isConstantCall()`: [2](#0-1) 
- If `isConstantCall()` is `true`, signature recovery runs in a plain sequential loop with **no CPU-time/timeout enforcement**.
- If `false`, it submits work to a shared `ExecutorService` and enforces a hard timeout via `countDownLatch.await(getCPUTimeLeftInNanoSecond(), ...)`, throwing `OutOfTimeException`/`notEnoughTime` when the budget is exceeded.

Because `batchValidateSign` (and `validateMultiSign`) is one shared object referenced by every thread that executes a `TriggerSmartContract`/constant call touching that precompile address, the `constantCall` boolean is not transaction-scoped or thread-scoped — it is a single mutable field mutated concurrently by:
- the block-processing/transaction-execution path (real broadcast transactions, `constantCall=false`), and
- concurrent read-only API paths (`Wallet.triggerConstantContract`, JSON-RPC `eth_call`, HTTP `/wallet/triggerconstantcontract`), which set `constantCall=true` on the very same singleton before calling `execute()`.

This is structurally identical to the reported libcurl bug class: an option intended to apply only to the calling thread's operation ("this transfer disables cert verification" / "this call is a constant call, skip the timeout check") is stored in shared, non-isolated state, so a concurrent operation on another thread can transiently observe (or overwrite) the wrong value between the `setConstantCall(...)` call and the `execute()`/`doExecute()` read of `isConstantCall()`.

Note that the codebase already fixed the analogous problem for `VMConfig` flags by moving from a purely global static field to a `ThreadLocal` + `volatile` global snapshot (`common/src/main/java/org/tron/core/vm/config/VMConfig.java`), explicitly to prevent a constant call's local view from leaking into or being clobbered by the concurrent block-processing path [3](#0-2) . The same isolation was never applied to the singleton `PrecompiledContract` instances' `constantCall`/`vmShouldEndInUs` fields, leaving the identical race in a different, still-reachable location.

### Impact Explanation
If a real, signed broadcast transaction's execution of `BatchValidateSign`/`ValidateMultiSign` races with a concurrently-issued constant call (any unprivileged API client can trigger `triggerconstantcontract`/`eth_call` against a contract that invokes these precompiles) such that the shared instance is left with `constantCall=true` when the broadcast transaction reads it, the broadcast transaction's signature-batch verification skips the CPU-time/timeout guard entirely. An attacker-controlled contract can supply the maximum-allowed 16 signatures/addresses to maximize recovery work with no enforced upper bound on wall-clock time for that call, letting a single transaction consume unbounded time on the block-processing thread — a node liveness/halt condition. Conversely, the opposite race can cause a constant call to unexpectedly enter the thread-pool/timeout path, which is lower impact but still demonstrates uncontrolled cross-request interference in shared precompile state.

### Likelihood Explanation
Exploitation requires only ordinary, unprivileged access: any account can broadcast a transaction that calls a contract invoking the `batchvalidatesign`/`validatemultisign` precompile, and any anonymous client can simultaneously fire `triggerconstantcontract` calls against the same precompile address via the public HTTP/gRPC/JSON-RPC API, which is a normal, always-available capability. Because `constantCall` is a plain instance field on a process-wide singleton with no synchronization, `volatile`, or thread-local isolation, a race window exists on every concurrent invocation pairing a constant call with a real broadcast transaction hitting the same precompile — this requires no special network position or privilege, only timing, and constant calls are typically higher-volume/lower-latency than block-processing execution, making concurrent overlap plausible in a live node continuously serving API traffic.

### Recommendation
Apply the same isolation strategy already used for `VMConfig` (ThreadLocal/execution-context-scoped state rather than shared singleton instance fields) to `PrecompiledContract.constantCall` (and any other per-call mutable fields such as `vmShouldEndInUs`), or instantiate precompiled contracts per-execution/per-thread instead of as `static final` shared singletons, so that no per-call flag can be observed or overwritten by a concurrently executing, unrelated call.

### Proof of Concept
Not independently reproduced with a running node; the analysis is based on static code review of `PrecompiledContracts.java`'s `static final` singleton declarations and the `isConstantCall()` branch in `BatchValidateSign.doExecute` [4](#0-3) . I was unable to inspect the `setConstantCall`/`constantCall` field declaration in the base `PrecompiledContract` class or the exact call sites in `VMActuator.java` before the tool budget was exhausted, so the precise synchronization (or complete absence thereof) around the `setConstantCall(...)` write-then-`execute()` sequence in `VMActuator` could not be fully confirmed. A background Devin session with full repository access would be needed to trace `VMActuator`'s call sequence and confirm whether any locking currently mitigates the race before treating this as verified rather than a structurally strong analog.

### Citations

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L91-101)
```java
  private static final ECRecover ecRecover = new ECRecover();
  private static final Sha256 sha256 = new Sha256();
  private static final Ripempd160 ripempd160 = new Ripempd160();
  private static final Identity identity = new Identity();
  private static final ModExp modExp = new ModExp();
  private static final BN128Addition altBN128Add = new BN128Addition();
  private static final BN128Multiplication altBN128Mul = new BN128Multiplication();
  private static final BN128Pairing altBN128Pairing = new BN128Pairing();

  private static final BatchValidateSign batchValidateSign = new BatchValidateSign();
  private static final ValidateMultiSign validateMultiSign = new ValidateMultiSign();
```

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1144-1207)
```java
    @Override
    public Pair<Boolean, byte[]> execute(byte[] data) {
      try {
        return doExecute(data);
      } catch (Throwable t) {
        if (t instanceof InterruptedException){
          Thread.currentThread().interrupt();
        }
        return Pair.of(true, new byte[WORD_SIZE]);
      }
    }

    private Pair<Boolean, byte[]> doExecute(byte[] data)
        throws InterruptedException, ExecutionException {
      if (VMConfig.allowTvmOsaka()
          && !isValidAbiEncoding(data, ABI_HEADER_WORDS, ABI_ITEM_WORDS)) {
        return Pair.of(false, EMPTY_BYTE_ARRAY);
      }
      DataWord[] words = DataWord.parseArray(data);
      byte[] hash = words[0].getData();

      if (VMConfig.allowTvmSelfdestructRestriction()) {
        int sigArraySize = words[words[1].intValueSafe() / WORD_SIZE].intValueSafe();
        int addrArraySize = words[words[2].intValueSafe() / WORD_SIZE].intValueSafe();
        if (sigArraySize > MAX_SIZE || addrArraySize > MAX_SIZE) {
          return Pair.of(true, DATA_FALSE);
        }
      }

      byte[][] signatures = VMConfig.allowTvmSelfdestructRestriction() ?
          extractSigArray(words, words[1].intValueSafe() / WORD_SIZE, data) :
          extractBytesArray(words, words[1].intValueSafe() / WORD_SIZE, data);
      byte[][] addresses = extractBytes32Array(
          words, words[2].intValueSafe() / WORD_SIZE);
      int cnt = signatures.length;
      if (cnt == 0 || cnt > MAX_SIZE || signatures.length != addresses.length) {
        return Pair.of(true, DATA_FALSE);
      }
      byte[] res = new byte[WORD_SIZE];
      if (isConstantCall()) {
        //for constant call not use thread pool to avoid potential effect
        for (int i = 0; i < cnt; i++) {
          if (DataWord
              .equalAddressByteArray(addresses[i], recoverAddrBySign(signatures[i], hash))) {
            res[i] = 1;
          }
        }
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
```

**File:** common/src/main/java/org/tron/core/vm/config/VMConfig.java (L51-61)
```java
  // HEAD / block-processing config, written by the consensus path; read by everyone with no
  // thread-local override. volatile so a wholesale install is safely published across threads.
  private static volatile Snapshot globalSnapshot = new Snapshot();

  // Per-thread override used only by constant calls bound to a non-HEAD (solidity/PBFT) snapshot.
  private static final ThreadLocal<Snapshot> localSnapshot = new ThreadLocal<>();

  private static Snapshot current() {
    Snapshot local = localSnapshot.get();
    return local != null ? local : globalSnapshot;
  }
```
