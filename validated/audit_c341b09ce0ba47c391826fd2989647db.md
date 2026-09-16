### Title
Shared, Unsynchronized Mutation of the TVM Opcode Dispatch Table Enables Cross-Request Contamination of Constant-Call Execution Semantics - (File: `actuator/src/main/java/org/tron/core/vm/OperationRegistry.java`)

### Summary
The Crafter CMS advisory (CWE-913, Improper Control of Dynamically-Managed Code Resources) describes a Groovy sandbox whose runtime-managed code-dispatch resource can be manipulated to execute logic outside its intended restriction boundary. The closest reachable analog in java-tron is the TVM's opcode dispatch table (`JumpTable`), which is likewise a dynamically-managed code resource: which `Operation` (enablement predicate, energy-cost function, and executable action) runs for a given opcode is decided at runtime from mutable, per-request `VMConfig` feature-flag state, and that state can legitimately differ between concurrently executing requests (HEAD vs. Solidity/PBFT snapshots). The dispatch table used for constant calls is a single static, unsynchronized, shared object, so one unprivileged API caller's opcode-table adjustment can bleed into another caller's concurrently executing view-call, changing which code resource (opcode enablement/cost/behavior) that other call actually runs.

### Finding Description
`OperationRegistry` keeps exactly one shared `JumpTable` instance for all constant-call executions: [1](#0-0) 

Every top-level VM execution — including every `triggerconstantcontract`/`eth_call`/`estimateEnergy` request from an anonymous API client — calls `prepareAndGetTable(isConstantCall)`, which mutates that shared table in place based on the *calling thread's current* `VMConfig` flags before executing any opcode: [2](#0-1) 

The mutation is a plain, unsynchronized array write with no memory barrier and no per-call isolation: [3](#0-2) 

and the adjustment functions directly branch on live config flags to decide which `Operation` variant (different cost function and/or different action, e.g. `DEFAULT_SUICIDE` vs `RESTRICTED_SUICIDE`, `DEFAULT_VOTEWITNESS` vs `OSAKA_VOTEWITNESS`) to install into the shared slot: [4](#0-3) 

Crucially, constant calls are explicitly designed to run with *different, request-specific* config views: a constant call bound to a non-HEAD (Solidity/PBFT) snapshot installs a **thread-local** `VMConfig.Snapshot` that can disagree with the global HEAD snapshot read by concurrent block-processing or by other concurrently executing constant calls: [5](#0-4) 

`triggerConstantContract`/`callConstantContract` runs on pooled RPC worker threads and is directly reachable by any unauthenticated JSON-RPC/gRPC/HTTP client: [6](#0-5) 

Because `CONSTANT_CALL_TABLE` is one object shared by *all* threads regardless of which `VMConfig` snapshot each thread is using, two constant calls running concurrently with different local snapshots (e.g., one still pre-fork, one post-fork) race on `table.set(...)`. `VM.play` then reads `op.isEnabled()`, `op.getEnergyCost(program)`, and `op.execute(program)` from that same shared slot at different points during opcode dispatch: [7](#0-6) 

with no guarantee that a writer's update to the slot is visible in a timely/consistent way to a concurrent reader, or that a reader's own `adjustTable()` call was the last one to run before its opcodes execute. The net effect is that the *opcode implementation actually invoked* for a given constant call is not solely determined by that call's own feature-flag view, but can be silently overwritten by whatever the last concurrently-running thread configured — i.e., the dynamically-managed code resource (the TVM opcode dispatch table) is not properly isolated per execution context, directly analogous to a sandbox/dispatch-table bypass.

### Impact Explanation
Any unprivileged client calling `triggerconstantcontract`, `eth_call`, or `estimateEnergy` concurrently with other such calls (including via `TriggerConstantContractOnSolidityServlet`/`TriggerConstantContractOnPBFTServlet`, which explicitly install differing thread-local snapshots) can have the SELFDESTRUCT/VOTEWITNESS/opcode-enablement/cost logic used for their own query silently swapped for a variant governed by an unrelated concurrent request's fork-activation state. This corrupts the correctness of the query-serving API path (energy estimates, view-call return values) served to arbitrary API clients, and is a genuine race on a shared mutable "code resource" whose selection logic should be strictly config-gated per execution — matching the CWE-913 bug class. Because constant-call results are not committed to chain state, the blast radius is confined to incorrect data returned by the query APIs (e.g., wrong `estimateEnergy`/`eth_estimateGas` results, wrong view-function return values) rather than direct fund loss, which keeps this at Medium severity rather than higher.

### Likelihood Explanation
The race requires only ordinary, unauthenticated concurrent usage of the constant-call RPC/HTTP surface while the network is near a fork-activation boundary or while Solidity/PBFT and HEAD state genuinely diverge (a normal, frequent operating condition) — no special privileges, malicious SR/witness status, or crafted contract bytecode is required, only ordinary concurrent client traffic.

### Recommendation
Do not share a single mutable `JumpTable` instance across concurrently executing constant calls that may run under different `VMConfig` snapshots. Either build a fresh, config-adjusted `JumpTable` per top-level execution (or per `VMConfig.Snapshot`), or make `OperationRegistry.adjustTable`/`JumpTable.set` immutable/copy-on-write and publish the adjusted table via a properly synchronized handoff (e.g., cache adjusted tables keyed by the resolved `Snapshot` rather than mutating one process-wide table in place).

### Proof of Concept
Deterministic reproduction requires concurrency, so this is best validated with an interleaving test: (1) Thread A calls `triggerConstantContract` bound to a PBFT/Solidity snapshot with `allowTvmSelfdestructRestriction=false` (so it expects `DEFAULT_SUICIDE`); (2) concurrently, Thread B calls `triggerConstantContract`/`estimateEnergy` on HEAD with `allowTvmSelfdestructRestriction=true`, causing `OperationRegistry.adjustSelfdestruct` to install `RESTRICTED_SUICIDE` into the shared `CONSTANT_CALL_TABLE` (`OperationRegistry.java:731-739`) between Thread A's `prepareAndGetTable` call and its opcode dispatch (`VM.java:38-90`); (3) observe that Thread A's SUICIDE opcode is executed using `suicideAction2`/`canSuicide2()` semantics instead of the `suicideAction`/`canSuicide()` semantics its own snapshot requested, demonstrating that a concurrent, unrelated caller controls which code resource executes for Thread A's request.

### Citations

**File:** actuator/src/main/java/org/tron/core/vm/OperationRegistry.java (L65-67)
```java
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

**File:** actuator/src/main/java/org/tron/core/vm/OperationRegistry.java (L721-739)
```java
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

**File:** actuator/src/main/java/org/tron/core/vm/config/ConfigLoader.java (L16-19)
```java
  // isolate=true: a constant call bound to a non-HEAD (solidity/PBFT) snapshot installs its
  // snapshot into a thread-local view instead of the process-wide global, so it cannot pollute
  // the flags the block-processing path reads concurrently.
  public static void load(StoreFactory storeFactory, boolean isolate) {
```

**File:** framework/src/main/java/org/tron/core/Wallet.java (L3139-3168)
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

**File:** actuator/src/main/java/org/tron/core/vm/VM.java (L38-90)
```java
        try {
          Operation op = jumpTable.get(program.getCurrentOpIntValue());
          if (!op.isEnabled()) {
            throw Program.Exception.invalidOpCode(program.getCurrentOp());
          }
          program.setLastOp((byte) op.getOpcode());

          /* stack underflow/overflow check */
          program.verifyStackSize(op.getRequire());
          program.verifyStackOverflow(op.getRequire(), op.getRet());

          String opName = Op.getNameOf(op.getOpcode());
          /* spend energy before execution */
          long energy = op.getEnergyCost(program);
          if (allowDynamicEnergy) {
            long actualEnergy = energy;
            // CALL Ops have special calculation on energy.
            if (CALL_OPS.contains(op.getOpcode())) {
              actualEnergy = energy
                  - program.getAdjustedCallEnergy().longValueSafe()
                  - program.getCallPenaltyEnergy();
            }
            energyUsage += actualEnergy;

            if (factor > DYNAMIC_ENERGY_FACTOR_DECIMAL) {
              long penalty;

              // CALL Ops have special calculation on energy.
              if (CALL_OPS.contains(op.getOpcode())) {
                penalty = program.getCallPenaltyEnergy();
              } else {
                penalty = energy * factor / DYNAMIC_ENERGY_FACTOR_DECIMAL - energy;
                if (penalty < 0) {
                  penalty = 0;
                }
                energy += penalty;
              }

              program.spendEnergyWithPenalty(energy, penalty, opName);
            } else {
              program.spendEnergy(energy, opName);
            }

          } else {
            program.spendEnergy(energy, opName);
          }


          /* check if cpu time out */
          program.checkCPUTimeLimit(opName);

          /* exec op action */
          op.execute(program);
```
