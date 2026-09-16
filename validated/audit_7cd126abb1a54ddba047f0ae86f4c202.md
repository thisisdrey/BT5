### Title
Unsynchronized concurrent mutation of the shared TVM `JumpTable` in `OperationRegistry` — data race on energy-metering/opcode state ([File: actuator/src/main/java/org/tron/core/vm/OperationRegistry.java])

### Summary
`OperationRegistry` keeps two **static, globally shared** `JumpTable` instances — the consensus (broadcast/block-processing) table and a dedicated constant-call table — and re-mutates their internal `Operation[]` in place on every single VM invocation via `prepareAndGetTable()`/`adjustTable()`, with no synchronization, volatile fields, or per-call copy-on-write. Because unauthenticated clients can trigger unlimited concurrent constant calls (`/wallet/triggerconstantcontract`, `/walletsolidity/triggerconstantcontract`, gRPC `triggerConstantContract`, `estimateEnergy`), many worker threads call `OperationRegistry.prepareAndGetTable(true)` in parallel, all writing into the same `CONSTANT_CALL_TABLE` array while other threads are concurrently reading `Operation` entries from it inside `VM.play()`. This is structurally the same bug class as the reported Aovec advisory: a shared mutable data structure used across threads without any `Send`/`Sync`-equivalent guarantee (in Java terms: no `synchronized`, no `volatile`, no immutable publication), causing a data race on production, transaction-reachable state.

### Finding Description
- `OperationRegistry` declares the shared tables as plain `static` fields: [1](#0-0) 
- `JumpTable` backs these with a plain, non-volatile `Operation[]` and unsynchronized `get`/`set`: [2](#0-1) 
- `prepareAndGetTable()` is the entry point every VM execution uses to fetch its table, and it unconditionally calls `adjustTable()`, which mutates the shared array (`adjustMemOperations`, `adjustVoteWitness`, `adjustSelfdestruct`) on **every call**, not just once at startup or on config change: [3](#0-2) 
- `CONSTANT_CALL_TABLE` is the single instance shared by *every* constant call on the node, regardless of caller or thread: [4](#0-3) 

Constant calls are reachable by anonymous clients through multiple concurrent channels: HTTP servlet threads, the gRPC `rpc-full-executor` pool, and PBFT/Solidity proxy futures, all funneling into `Wallet.callConstantContract()`, which builds a fresh `VMActuator(true)` per call and executes it without any lock around table adjustment: [5](#0-4) [6](#0-5) [7](#0-6) 

The test suite (`VMActuatorMockTest`, `OperationRegistryTest`) documents the *intent* of the isolation (dedicated constant-call table vs. shared consensus table) but only verifies single-threaded reference identity — it never validates concurrent-write safety of `adjustTable()`/`JumpTable.set()`, confirming the underlying array mutation path itself is not synchronized: [8](#0-7) 

Because `Operation` instances carry an energy-cost function and an `isEnabled()` gate (`BooleanSupplier`) that get *replaced* in the shared array based on the currently observed `VMConfig` flags (e.g., `allowHigherLimitForMaxCpuTimeOfOneTx`, `allowTvmOsaka`, `allowEnergyAdjustment`, `allowTvmSelfdestructRestriction`), two threads racing to update/read the same opcode slot can cause one thread's TVM execution to read an `Operation` object that reflects a different (stale or transiently-inconsistent) configuration than the one intended for that execution's snapshot — a straightforward unsynchronized shared-mutable-state race, matching CWE-662 (insufficient synchronization) directly, and reachable purely from unprivileged/anonymous JSON-RPC/gRPC traffic (`triggerConstantContract`, `estimateEnergy`).

### Impact Explanation
The race sits on the code path that determines **per-opcode energy cost and opcode enablement** for every TVM execution (both constant calls and, via the analogous shared consensus `tableMap` table, ordinary broadcast/block-application executions). A successful race can cause:
- Incorrect energy metering (wrong cost function applied to `MLOAD`/`MSTORE`/`MSTORE8`/`VOTEWITNESS`/`SUICIDE`) for a given execution, undermining the energy-accounting guarantees the rules explicitly flag as in-scope.
- Because the array holds live object references mutated without a memory barrier, another thread is not guaranteed to observe an update in a timely or complete fashion under the JMM, which can produce inconsistent behavior between concurrently-running constant calls that should be using different opcode variants (e.g., restricted vs. unrestricted `SUICIDE` action, or v2 vs. v3 `VOTEWITNESS` cost).

This does not itself expose a documented direct fund-theft primitive from the code visible here, so it is rated as a data-integrity/availability concern in the TVM metering path rather than a confirmed unauthorized-transfer bug.

### Likelihood Explanation
High reachability: any anonymous client can call `triggerconstantcontract`/`estimateenergy` over HTTP or gRPC with no signature or fee requirement, and these are served from thread pools sized for concurrency (`rpc-full-executor`, Jetty worker threads). Every single call re-executes `adjustTable()` against the same shared static table, so the race window opens on essentially every request; no special timing beyond ordinary request concurrency is required to trigger overlapping reads/writes on the shared array.

### Recommendation
- Build each execution's `JumpTable` as an immutable, per-call (or per-config-snapshot, correctly copy-on-write) instance rather than mutating the shared static `CONSTANT_CALL_TABLE` / `tableMap` arrays in place on every call.
- If a shared cache of pre-adjusted tables is desired, key it by the exact config snapshot and publish new tables via `volatile`/`AtomicReference` with full construction before publication, and avoid any in-place `Operation[]` mutation after publication.
- Add a concurrency test (multiple threads calling `prepareAndGetTable(true)`/`(false)` concurrently while `VM.play()` reads from the returned tables) to catch regressions.

### Proof of Concept
1. Deploy a contract exposing a view function.
2. Fire a sustained burst of concurrent `triggerconstantcontract`/`estimateenergy` requests (HTTP and/or gRPC) from multiple threads/clients against the same node.
3. Instrument (or run under a race detector / heavily loaded environment) `OperationRegistry.adjustMemOperations/adjustVoteWitness/adjustSelfdestruct` and `JumpTable.set/get` to show interleaved writes/reads on the same array without synchronization, and observe threads occasionally executing with an `Operation` variant inconsistent with the config visible to that thread at call time (energy cost or opcode-enablement mismatch across concurrently executing constant calls).

Note: I was not able to view the exact call site inside `VMActuator.java` where `OperationRegistry.prepareAndGetTable`/`getTable` is invoked (grep for that specific file returned no direct match, likely due to index truncation), so the precise line linking `VMActuator.execute()` to `OperationRegistry` could not be cited directly; this is inferred from `VMActuatorMockTest` assertions and `OperationRegistry`'s public API, and should be confirmed by reading `actuator/src/main/java/org/tron/core/actuator/VMActuator.java` directly in a full checkout.

### Citations

**File:** actuator/src/main/java/org/tron/core/vm/OperationRegistry.java (L55-67)
```java
  private static final Map<Version, JumpTable> tableMap = new HashMap<>();

  // The newest version in use. Bump this when a newer operation set is added,
  // together with newLatestOperationSet() below.
  private static final Version LATEST_VERSION = Version.TRON_V1_5;

  static {
    tableMap.put(LATEST_VERSION, newLatestOperationSet());
  }

  // Constant calls get a dedicated instance of the newest table, isolated from
  // the shared consensus table above.
  private static final JumpTable CONSTANT_CALL_TABLE = newLatestOperationSet();
```

**File:** actuator/src/main/java/org/tron/core/vm/OperationRegistry.java (L118-134)
```java
  public static JumpTable prepareAndGetTable(boolean isConstantCall) {
    JumpTable table = getTable(isConstantCall);
    // Apply configuration-dependent changes once at the top level.
    adjustTable(table);
    return table;
  }

  public static JumpTable getTable(boolean isConstantCall) {
    return isConstantCall ? CONSTANT_CALL_TABLE : tableMap.get(LATEST_VERSION);
  }

  private static void adjustTable(JumpTable table) {
    // Make the corresponding changes, excluding opcode activation.
    adjustMemOperations(table);
    adjustVoteWitness(table);
    adjustSelfdestruct(table);
  }
```

**File:** actuator/src/main/java/org/tron/core/vm/JumpTable.java (L14-27)
```java
  private final Operation[] table = new Operation[256];

  public JumpTable() {
    // fill all op slots to undefined
    Arrays.fill(table, UNDEFINED);
  }
  
  public Operation get(int op) {
    return table[op];
  }

  public void set(Operation op) {
    table[op.getOpcode()] = op;
  }
```

**File:** framework/src/main/java/org/tron/core/Wallet.java (L3159-3168)
```java
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

**File:** framework/src/main/java/org/tron/core/services/http/TriggerConstantContractServlet.java (L59-66)
```java
      TransactionCapsule trxCap = wallet
          .createTransactionCapsule(build.build(), ContractType.TriggerSmartContract);

      Transaction trx = wallet
          .triggerConstantContract(build.build(),trxCap,
              trxExtBuilder,
              retBuilder);
      trx = Util.setTransactionPermissionId(jsonObject, trx);
```

**File:** framework/src/main/java/org/tron/core/services/RpcApiService.java (L215-227)
```java
  private void callContract(TriggerSmartContract request,
      StreamObserver<TransactionExtention> responseObserver, boolean isConstant) {
    TransactionExtention.Builder trxExtBuilder = TransactionExtention.newBuilder();
    Return.Builder retBuilder = Return.newBuilder();
    try {
      TransactionCapsule trxCap = createTransactionCapsule(request,
          ContractType.TriggerSmartContract);
      Transaction trx;
      if (isConstant) {
        trx = wallet.triggerConstantContract(request, trxCap, trxExtBuilder, retBuilder);
      } else {
        trx = wallet.triggerContract(request, trxCap, trxExtBuilder, retBuilder);
      }
```

**File:** framework/src/test/java/org/tron/core/vm/OperationRegistryTest.java (L37-59)
```java
  @Test
  public void constantAdjustmentsDoNotMutateTransactionTable() {
    boolean previousHigherLimit = VMConfig.allowHigherLimitForMaxCpuTimeOfOneTx();
    JumpTable transactionTable = OperationRegistry.getTable(false);
    JumpTable constantCallTable = OperationRegistry.getTable(true);
    try {
      VMConfig.initAllowHigherLimitForMaxCpuTimeOfOneTx(0);
      OperationRegistry.adjustMemOperations(transactionTable);
      OperationRegistry.adjustMemOperations(constantCallTable);
      Operation transactionMload = transactionTable.get(Op.MLOAD);
      Operation constantMload = constantCallTable.get(Op.MLOAD);

      VMConfig.initAllowHigherLimitForMaxCpuTimeOfOneTx(1);
      OperationRegistry.adjustMemOperations(constantCallTable);

      assertSame(transactionMload, transactionTable.get(Op.MLOAD));
      assertNotSame(constantMload, constantCallTable.get(Op.MLOAD));
    } finally {
      VMConfig.initAllowHigherLimitForMaxCpuTimeOfOneTx(previousHigherLimit ? 1 : 0);
      OperationRegistry.adjustMemOperations(transactionTable);
      OperationRegistry.adjustMemOperations(constantCallTable);
    }
  }
```
