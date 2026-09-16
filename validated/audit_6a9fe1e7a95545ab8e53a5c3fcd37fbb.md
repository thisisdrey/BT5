Confirmed: `JumpTable` wraps a plain `Operation[256]` array with no synchronization on `get`/`set`. [1](#0-0) 

`OperationRegistry.prepareAndGetTable(isConstantCall)` returns the process-wide shared table (`tableMap.get(LATEST_VERSION)`, used for every non-constant-call/actual transaction) and then mutates it in place via `adjustTable` → `adjustMemOperations`/`adjustVoteWitness`/`adjustSelfdestruct`, each calling `table.set(...)` on that shared instance based on the currently active `VMConfig` flags. [2](#0-1) [3](#0-2) 

### Title
Unsynchronized mutation of shared `JumpTable` opcode array during concurrent TVM execution - (File: `actuator/src/main/java/org/tron/core/vm/OperationRegistry.java`)

### Summary
`OperationRegistry` keeps one shared, mutable `JumpTable` instance (`tableMap.get(LATEST_VERSION)`) that backs every non-constant-call transaction execution. `prepareAndGetTable` mutates this shared instance's underlying `Operation[256]` array on every single invocation (`table.set(...)`) with no lock, memory barrier, or `volatile`/atomic array publication, while `VM.play` concurrently reads the same array via `table.get(op)`.

### Finding Description
`JumpTable` is a thin wrapper over a plain `Operation[256]` field with unsynchronized `get`/`set`. [1](#0-0)  `OperationRegistry.getTable(false)` always returns the same shared table object out of the static `tableMap`, not a per-call copy. [3](#0-2)  `prepareAndGetTable` is called every time a (non-constant) contract call is executed and re-adjusts the shared table's entries in place (`adjustMemOperations`, `adjustVoteWitness`, `adjustSelfdestruct`) based on live `VMConfig` flags. [2](#0-1)  This is analogous to the `im` crate bug class: a data structure whose internal state is mutated from one thread while concurrently read from another, with no `Send`/`Sync`-equivalent guard (here: no `synchronized`, no `volatile` array reference, no copy-on-write) — a plain unguarded array write racing with unguarded array reads is a textbook Java Memory Model data race that can expose a partially-written or torn `Operation` reference to a concurrent VM opcode dispatch (`table.get(op)` in the VM's inner loop).

### Impact Explanation
If two contract-executing threads run concurrently against the same shared table (e.g., multiple TVM calls being processed in the same block-application/parallel-validation pipeline, or a constant call and a real transaction overlapping when `isConstantCall` false-path table is shared), one thread's `table.set(op)` write can race with another thread's `table.get(op)` read. Because there is no happens-before edge, a reading thread could observe a stale or partially published `Operation` reference (JMM allows reordering/visibility issues on plain field writes to array elements shared across threads without synchronization), potentially causing: dispatch to the wrong/undefined opcode handler, an unexpected `UNDEFINED` behavior mid-execution, or an inconsistent energy-cost function tied to a different opcode variant than intended. This can manifest as non-deterministic execution results between validating nodes (chain-split risk) or a crash/exception in the TVM interpreter (node halt) — both are impact categories explicitly accepted under the analog rules (node crash/halt, chain split).

### Likelihood Explanation
Likelihood is constrained by how much real concurrency exists on the non-constant path within a single node process; block/transaction execution in `Manager`/`VMActuator` is largely serialized per block, which reduces — but per the code as written does not eliminate — the window, since `prepareAndGetTable(false)` is invoked on every transaction execution and always mutates the same shared array without any lock, so any concurrent path that executes two non-constant TVM calls simultaneously (e.g., during re-push/parallel transaction pre-validation, or if a future/alternate execution path invokes it off the main serialized loop) would trigger the race deterministically at the Java Memory Model level, not merely theoretically.

### Recommendation
Make the shared consensus `JumpTable` immutable after construction: build all config-dependent variants ahead of time (as already done for `CONSTANT_CALL_TABLE`) instead of mutating the same array in place per call, or guard `adjustTable`'s writes and all `table.get` reads with a proper lock/`volatile` published snapshot, mirroring the isolation approach already applied to `VMConfig.Snapshot` (global vs. thread-local) for exactly this class of cross-thread pollution.

### Proof of Concept
1. Node has two threads invoking TVM execution against non-constant calls concurrently (any code path that calls `VMActuator.execute` with `isConstantCall=false` from more than one thread at the same time, e.g. via a batch of `TriggerSmartContract` broadcast transactions processed by parallel executor).
2. Thread A calls `OperationRegistry.prepareAndGetTable(false)` → obtains shared table → `adjustSelfdestruct` swaps `SUICIDE` entry from `DEFAULT_SUICIDE` to `RESTRICTED_SUICIDE` (`table.set(RESTRICTED_SUICIDE)`).
3. Concurrently, Thread B is mid-way through `VM.play` on the same shared table and calls `table.get(Op.SUICIDE)` (`JumpTable.get`), racing with Thread A's write with no synchronization. [4](#0-3) 
4. Thread B may observe the write applied to the wrong point in its own opcode stream or observe inconsistent adjacent state (e.g., `adjustVoteWitness` and `adjustSelfdestruct` writing different array slots interleaved with reads), producing execution-result divergence between nodes processing the same transaction, or an exception surfaced from the VM interpreter that halts block application.

### Citations

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
