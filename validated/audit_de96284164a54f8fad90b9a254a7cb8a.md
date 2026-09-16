### Title
Unsynchronized in-place mutation of the shared `JumpTable` in `OperationRegistry` causes a data race between concurrent TVM executions - ([File: actuator/src/main/java/org/tron/core/vm/OperationRegistry.java])

### Summary
`OperationRegistry` keeps one shared, mutable `JumpTable` per execution mode (the consensus table in `tableMap` and `CONSTANT_CALL_TABLE` for constant calls) and mutates it in place on every single call via `prepareAndGetTable()`/`adjustTable()`, with no lock, no `volatile`, and no thread-confinement. This mirrors the `aovec` bug class (a shared mutable container reachable by multiple threads with no `Send`/`Sync`-equivalent guarantees), except here the unsynchronized writer is Java code writing into a shared `Operation[256]` array while other threads read/write the very same array concurrently.

### Finding Description
`OperationRegistry.prepareAndGetTable(boolean isConstantCall)` returns a *shared, mutable* `JumpTable` instance and immediately mutates it based on live `VMConfig` flags: [1](#0-0) 

`getTable()` hands back one of two process-wide singletons — `tableMap.get(LATEST_VERSION)` (used for real, consensus-affecting transaction execution) or `CONSTANT_CALL_TABLE` (used for read-only/query calls): [2](#0-1) 

`adjustTable()` calls `table.set(...)` for `MLOAD/MSTORE/MSTORE8`, `VOTEWITNESS`, and `SUICIDE` depending on the caller's currently-active `VMConfig` flags: [3](#0-2) 

The backing store is a plain, non-volatile `Operation[256]` array with a simple, unsynchronized write: [4](#0-3) 

Because the exact same `JumpTable` object is shared by every transaction/call of a given mode, and `prepareAndGetTable()` is invoked from `VMActuator` on every single execution, any two threads executing VM code concurrently for the same mode (constant vs. consensus) race on the same backing array with no synchronization. In java-tron, multiple threads can legitimately call into `VMActuator`/`OperationRegistry` at the same time:
- Multiple concurrent constant-call (query) requests (`triggerConstantContract`, `EstimateEnergy`, TronJsonRpc `eth_call`) each invoke `prepareAndGetTable(true)` against the single shared `CONSTANT_CALL_TABLE`.
- The main block-application thread executing transactions during `pushBlock`/`processTransaction` and the separate `repush` `ExecutorService` thread (`Manager.rePushLoop`) can both invoke `prepareAndGetTable(false)` against the single shared consensus `tableMap` entry at the same time, since re-push runs on its own dedicated executor independent from block application: [5](#0-4) 

When two threads race on `table.set(...)` for the *same* opcode slot (e.g. one thread installing `DEFAULT_SUICIDE` while another installs `RESTRICTED_SUICIDE`/`ADJUSTED_SUICIDE`, or one installing `DEFAULT_MLOAD` while another installs `ADJUSTED_MLOAD`), a concurrently-executing VM interpreter loop reading `table.get(op)` for that opcode can observe whichever operation "wins" the race — independent of which `VMConfig` state that particular execution/thread should actually be using. This is a classic unsynchronized-shared-mutable-state data race: no happens-before relationship links the writer thread's `adjustTable()` call to the reader thread's `table.get(op)` call, so under the JMM the read may see a stale, torn, or unexpected `Operation` reference (energy-cost function and/or action function mismatched to what that particular execution context assumed).

### Impact Explanation
For the consensus table, a race between the block-application thread and the re-push thread can make a transaction execute with the *wrong* opcode cost function or the *wrong* action implementation for `MLOAD/MSTORE/MSTORE8`, `VOTEWITNESS`, or `SUICIDE` (e.g. using `RESTRICTED_SUICIDE`'s `suicideAction2` instead of the plain `suicideAction`, or an adjusted vs. default energy cost). This can cause incorrect energy accounting or an incorrect state transition during real block application — a correctness/consensus-determinism concern, since two different java-tron nodes could, under different concurrent scheduling, apply the same block with subtly different opcode semantics for the same transaction. For the constant-call table, concurrent API query threads (`triggerConstantContract`, `EstimateEnergy`, JSON-RPC `eth_call`) can similarly corrupt each other's `CONSTANT_CALL_TABLE`, producing incorrect energy estimates or incorrect execution results returned to callers of the query API.

### Likelihood Explanation
The race requires a genuine timing overlap: for the consensus table this is transaction execution overlapping with the dedicated `repush` executor thread's own transaction re-validation, or two overlapping requests to `prepareAndGetTable(false)`; for the constant-call table it only requires two concurrent `triggerConstantContract`/`eth_call`/`EstimateEnergy` requests, which any unprivileged API client can trivially trigger by firing overlapping RPC calls. No special privileges are needed — an anonymous API client can reach the constant-call path, and any broadcaster/re-pusher of pending transactions can trigger the consensus-table path.

### Recommendation
Do not mutate the shared, process-wide `JumpTable` singletons per-call. Instead, either:
1. Make `adjustTable()` produce/return a fresh, thread-confined copy of the table (or precompute the small set of variant tables up front, one per `VMConfig` flag combination, and select immutably), or
2. Protect the shared tables with proper synchronization (e.g., `synchronized`/`ReentrantLock` around read+mutate, or replace `Operation[]` with an array of `AtomicReference<Operation>` and publish the whole snapshot atomically), ensuring no reader ever observes a table mid-mutation from another thread.

### Proof of Concept
1. Deploy/trigger two concurrent constant-call executions (e.g., two parallel `triggerConstantContract` HTTP requests) whose active `VMConfig` snapshots differ in `allowTvmSelfdestructRestriction`/`allowEnergyAdjustment` (achievable across a chain-parameter-proposal activation boundary, or by crafting two calls bound to different historical/solidity snapshots via `VMConfig.setLocalSnapshot`).
2. Both calls invoke `OperationRegistry.prepareAndGetTable(true)`, which calls `adjustTable(CONSTANT_CALL_TABLE)` and mutates the single shared `CONSTANT_CALL_TABLE.set(...)` for `SUICIDE`/`MLOAD`/etc. concurrently on separate threads.
3. Because `JumpTable.table` is a plain array with no synchronization, one call's VM execution can end up reading the *other* call's opcode variant (e.g., `RESTRICTED_SUICIDE` instead of `ADJUSTED_SUICIDE`) mid-execution, producing incorrect energy costs/results.
4. The same race window exists for the consensus `tableMap` entry between `Manager`'s block-application thread and the `repush` executor thread (`Manager.rePushLoop`), both of which call into `VMActuator`→`OperationRegistry.prepareAndGetTable(false)` independently and concurrently.

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

**File:** actuator/src/main/java/org/tron/core/vm/OperationRegistry.java (L714-739)
```java
  public static void adjustMemOperations(JumpTable table) {
    boolean adjusted = VMConfig.allowHigherLimitForMaxCpuTimeOfOneTx();
    table.set(adjusted ? ADJUSTED_MLOAD : DEFAULT_MLOAD);
    table.set(adjusted ? ADJUSTED_MSTORE : DEFAULT_MSTORE);
    table.set(adjusted ? ADJUSTED_MSTORE8 : DEFAULT_MSTORE8);
  }

  public static void adjustVoteWitness(JumpTable table) {
    if (VMConfig.allowTvmOsaka()) {
      table.set(OSAKA_VOTEWITNESS);
    } else if (VMConfig.allowEnergyAdjustment()) {
      table.set(ADJUSTED_VOTEWITNESS);
    } else {
      table.set(DEFAULT_VOTEWITNESS);
    }
  }

  public static void adjustSelfdestruct(JumpTable table) {
    if (VMConfig.allowTvmSelfdestructRestriction()) {
      table.set(RESTRICTED_SUICIDE);
    } else if (VMConfig.allowEnergyAdjustment()) {
      table.set(ADJUSTED_SUICIDE);
    } else {
      table.set(DEFAULT_SUICIDE);
    }
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

**File:** framework/src/main/java/org/tron/core/db/Manager.java (L286-319)
```java
  /**
   * Cycle thread to rePush Transactions
   */
  private Runnable rePushLoop =
      () -> {
        while (isRunRePushThread) {
          TransactionCapsule tx = null;
          try {
            tx = getRePushTransactions().peek();
            if (tx != null) {
              this.rePush(tx);
            } else {
              TimeUnit.MILLISECONDS.sleep(SLEEP_TIME_OUT);
            }
          } catch (Throwable ex) {
            if (ex instanceof InterruptedException) {
              Thread.currentThread().interrupt();
            }
            logger.error("Unknown exception happened in rePush loop.", ex);
            if (tx != null) {
              Metrics.counterInc(MetricKeys.Counter.TXS, 1,
                  MetricLabels.Counter.TXS_FAIL, MetricLabels.Counter.TXS_FAIL_ERROR);
            }
            ExitManager.findTronError(ex).ifPresent(e -> {
              throw e;
            });
          } finally {
            if (tx != null && getRePushTransactions().remove(tx)) {
              Metrics.gaugeInc(MetricKeys.Gauge.MANAGER_QUEUE, -1,
                  MetricLabels.Gauge.QUEUE_REPUSH);
            }
          }
        }
      };
```
