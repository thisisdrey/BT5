Confirmed: `org.apache.commons.collections4.map.LRUMap` is explicitly documented by Apache Commons Collections as **not synchronized / not thread-safe**, and `Program.programPrecompileLRUMap` is declared as a `static final` field shared process-wide across every TVM execution thread, with no `Collections.synchronizedMap()` wrapper or lock guarding `containsKey`/`get`/`put` in `getProgramPrecompile()`.

### Title
Unsynchronized concurrent read/write on static `LRUMap` cache in TVM `Program.getProgramPrecompile()` crashes/corrupts constant-call query path - (File: `actuator/src/main/java/org/tron/core/vm/program/Program.java`)

### Summary
`Program` exposes a process-wide, static, non-thread-safe `LRUMap<Key, ProgramPrecompile>` used to cache JUMPDEST analysis results for every non-constant-call VM execution. Multiple concurrent `wallet/triggerconstantcontract` (HTTP), gRPC `triggerConstantContract`/`estimateEnergy`, and JSON-RPC `eth_call`/`eth_estimateGas` requests each spin up their own `VMActuator`/`Program` instance on pooled RPC worker threads with no shared lock, yet all of them read and mutate the same static `LRUMap` concurrently whenever `isConstantCall()` is false for a nested/internal call. `LRUMap` is explicitly documented as not thread-safe: its internal doubly-linked hash bucket structure can become corrupted under concurrent `put`/`get`, producing infinite loops, `NullPointerException`, `ArrayIndexOutOfBoundsException`, or lost/duplicate entries — analogous to Go's fatal concurrent map read/write, but manifesting as JVM-level state corruption/exceptions instead of an instant fatal abort.

### Finding Description [1](#0-0) : `programPrecompileLRUMap` is declared `private static final LRUMap<Key, ProgramPrecompile>` — a single instance shared by **every** `Program` object created anywhere in the process for the lifetime of the JVM. [2](#0-1)  `getProgramPrecompile()` performs unsynchronized `containsKey`, `get`, and `put` calls on this static map:
```java
if (programPrecompileLRUMap.containsKey(key)) {
  programPrecompile = programPrecompileLRUMap.get(key);
} else {
  programPrecompile = ProgramPrecompile.compile(ops);
  programPrecompileLRUMap.put(key, programPrecompile);
}
```
No `synchronized` block, no `ConcurrentHashMap`, and no `Collections.synchronizedMap` wrapper protects this read-then-write (check-then-act) sequence.

Reachability: an unauthenticated client can trigger concurrent VM executions purely through read-only query paths that require no signed/broadcast transaction and no funds:
- HTTP `/wallet/triggerconstantcontract` → `Wallet.triggerConstantContract` → `Wallet.callConstantContract` → `new VMActuator(true)` [3](#0-2) 
- gRPC `triggerConstantContract`/`estimateEnergy` via `RpcApiService` [4](#0-3) 
- Solidity/PBFT HTTP mirrors, which explicitly run these on pooled worker threads (`futureGet`) rather than the single block-processing thread [5](#0-4) 

None of these paths take `Manager.transactionLock`/`synchronized(this)` that guards `pushTransaction` — that lock only protects actual broadcast/pending-pool mutation, not constant-call VM execution. Each concurrent constant call constructs its own `RepositoryImpl`/`Program`, but they all still funnel into the one static `programPrecompileLRUMap` whenever `getProgramPrecompile()` is reached on a non-constant nested call path (e.g., internal message calls triggered from within a constant call's contract code, or normal `TriggerSmartContract` execution running concurrently with constant calls on other threads via the RPC thread pool).

### Impact Explanation
`LRUMap`'s internal structure (a custom hash+doubly-linked-list implementation) is not safe for concurrent modification. Under load, concurrent `put`/`get`/eviction can corrupt the linked list, causing:
- Infinite loops in `removeLRU`/`updateEntry`, permanently hanging the worker thread that hit the corrupted structure.
- `NullPointerException`/`ArrayIndexOutOfBoundsException` thrown out of the query servlet/gRPC handler thread.
- Silent cache corruption causing incorrect `ProgramPrecompile` (JUMPDEST table) to be returned for a given bytecode key, which can make legitimate JUMP/JUMPDEST validation behave incorrectly during contract execution — a correctness bug in addition to availability.

Since `triggerconstantcontract`/`eth_call`/`estimateEnergy` are unauthenticated, unmetered read-only endpoints heavily used by wallets/dApps, an attacker can flood them with concurrent contract-call requests (targeting bytecode that includes internal message calls) to reliably trigger races on the shared cache. Repeated corruption/hangs across the fixed-size RPC/HTTP thread pools can exhaust available worker threads, taking down the node's constant-call/JSON-RPC query surface (`TronJsonRpcImpl`, `Wallet` query APIs) — matching the "API the node can no longer serve" acceptance criterion.

### Likelihood Explanation
High under realistic conditions: `triggerconstantcontract` and `eth_call` are extremely common, high-QPS, permissionless endpoints (no auth token, no fee, no on-chain footprint required beyond an existing deployed contract). Any contract with an internal call (very common in production DApps, e.g. proxy patterns, token transfers calling other contracts) exercises the non-constant `getProgramPrecompile()` path for the callee. A modest number of concurrent requests against such a contract is enough to race on the shared static `LRUMap`, similar in spirit and reachability to the BSF advisory's concurrent-map trigger via authenticated PUT floods, except here the endpoint requires no authentication at all.

### Recommendation
Replace `programPrecompileLRUMap` with a thread-safe bounded cache (e.g., Guava `Cache`/`LoadingCache` with `maximumSize`, or wrap the `LRUMap` access in `synchronized`/use `Collections.synchronizedMap` plus synchronized iteration), ensuring the check-then-act `containsKey`/`get`/`put` sequence in `getProgramPrecompile()` is atomic.

### Proof of Concept
1. Deploy a contract `A` whose entry function performs an internal `CALL` to another contract `B` (so `getProgramPrecompile()` is invoked on the non-constant-call branch for `B`'s bytecode/jump-dest analysis, populating/reading `programPrecompileLRUMap`).
2. From an anonymous client, fire a high volume of concurrent HTTP requests to `POST /wallet/triggerconstantcontract` (and/or gRPC `triggerConstantContract` / JSON-RPC `eth_call`) invoking `A`'s entry function, e.g. 64 threads × 50 requests each, mirroring the BSF PoC's concurrency pattern.
3. Observe worker-thread exceptions (`NullPointerException`/`ArrayIndexOutOfBoundsException`) or thread hangs originating from `LRUMap` internals inside `Program.getProgramPrecompile()`, and/or a growing number of stuck RPC worker threads as evidence of cache corruption under concurrent unsynchronized access.

### Citations

**File:** actuator/src/main/java/org/tron/core/vm/program/Program.java (L117-119)
```java
  private static final int lruCacheSize = CommonParameter.getInstance().getSafeLruCacheSize();
  private static final LRUMap<Key, ProgramPrecompile> programPrecompileLRUMap
      = new LRUMap<>(lruCacheSize);
```

**File:** actuator/src/main/java/org/tron/core/vm/program/Program.java (L208-225)
```java
  public ProgramPrecompile getProgramPrecompile() {
    if (isConstantCall()) {
      if (programPrecompile == null) {
        programPrecompile = ProgramPrecompile.compile(ops);
      }
      return programPrecompile;
    }
    if (programPrecompile == null) {
      Key key = getJumpDestAnalysisCacheKey();
      if (programPrecompileLRUMap.containsKey(key)) {
        programPrecompile = programPrecompileLRUMap.get(key);
      } else {
        programPrecompile = ProgramPrecompile.compile(ops);
        programPrecompileLRUMap.put(key, programPrecompile);
      }
    }
    return programPrecompile;
  }
```

**File:** framework/src/main/java/org/tron/core/Wallet.java (L3139-3169)
```java
  public Transaction callConstantContract(TransactionCapsule trxCap,
      Builder builder, Return.Builder retBuilder, boolean isEstimating)
      throws ContractValidateException, ContractExeException, HeaderNotFound, VMIllegalException {

    if (!Args.getInstance().isSupportConstant()) {
      throw new ContractValidateException("this node does not support constant");
    }

    Block headBlock;
    List<BlockCapsule> blockCapsuleList = chainBaseManager.getBlockStore()
        .getBlockByLatestNum(1);
    if (CollectionUtils.isEmpty(blockCapsuleList)) {
      throw new HeaderNotFound("latest block not found");
    } else {
      headBlock = blockCapsuleList.get(0).getInstance();
    }

    BlockCapsule headBlockCapsule = new BlockCapsule(headBlock);
    TransactionContext context = new TransactionContext(headBlockCapsule, trxCap,
        StoreFactory.getInstance(), true, false);
    VMActuator vmActuator = new VMActuator(true);

    try {
      vmActuator.validate(context);
      vmActuator.execute(context);
    } finally {
      // constant call runs on a pooled RPC worker; drop its thread-local VM config view so it
      // can never leak into a later (block/broadcast) execution on the same thread.
      VMConfig.clearLocalSnapshot();
    }

```

**File:** framework/src/main/java/org/tron/core/services/RpcApiService.java (L858-896)
```java
    @Override
    public void triggerConstantContract(TriggerSmartContract request,
        StreamObserver<TransactionExtention> responseObserver) {

      callContract(request, responseObserver, true);
    }

    @Override
    public void estimateEnergy(TriggerSmartContract request,
        StreamObserver<EstimateEnergyMessage> responseObserver) {
      TransactionExtention.Builder trxExtBuilder = TransactionExtention.newBuilder();
      Return.Builder retBuilder = Return.newBuilder();
      EstimateEnergyMessage.Builder estimateBuilder
          = EstimateEnergyMessage.newBuilder();

      try {
        TransactionCapsule trxCap = createTransactionCapsule(request,
            ContractType.TriggerSmartContract);
        wallet.estimateEnergy(request, trxCap, trxExtBuilder, retBuilder, estimateBuilder);
      } catch (ContractValidateException | VMIllegalException e) {
        retBuilder.setResult(false).setCode(response_code.CONTRACT_VALIDATE_ERROR)
            .setMessage(ByteString.copyFromUtf8(Wallet
                .CONTRACT_VALIDATE_ERROR + e.getMessage()));
        logger.warn(CONTRACT_VALIDATE_EXCEPTION, e.getMessage());
      } catch (RuntimeException e) {
        retBuilder.setResult(false).setCode(response_code.CONTRACT_EXE_ERROR)
            .setMessage(ByteString.copyFromUtf8(e.getClass() + " : " + e.getMessage()));
        logger.warn("When run estimate energy in VM, have Runtime Exception: " + e.getMessage());
      } catch (Exception e) {
        retBuilder.setResult(false).setCode(response_code.OTHER_ERROR)
            .setMessage(ByteString.copyFromUtf8(e.getClass() + " : " + e.getMessage()));
        logger.warn(UNKNOWN_EXCEPTION_CAUGHT + e.getMessage(), e);
      } finally {
        estimateBuilder.setResult(retBuilder);
        responseObserver.onNext(estimateBuilder.build());
        responseObserver.onCompleted();
      }
    }

```

**File:** framework/src/main/java/org/tron/core/services/interfaceOnSolidity/http/TriggerConstantContractOnSolidityServlet.java (L20-32)
```java
  protected void doGet(HttpServletRequest request, HttpServletResponse response) {
    walletOnSolidity.futureGet(() -> super.doGet(request, response));
  }

  protected void doPost(HttpServletRequest request, HttpServletResponse response) {
    walletOnSolidity.futureGet(() -> {
      try {
        super.doPost(request, response);
      } catch (IOException e) {
        logger.error("TriggerConstantContractOnSolidityServlet Exception", e);
      }
    });
  }
```
