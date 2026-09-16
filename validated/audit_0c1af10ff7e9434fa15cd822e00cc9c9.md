## Title
Dynamic Energy Cost model uses same-block cumulative usage to gate the cycle-transition factor update, allowing manipulation of TVM call costs - ([File: chainbase/src/main/java/org/tron/core/capsule/ContractStateCapsule.java])

## Summary
`ContractStateCapsule.catchUpToCycle` implements TRON's per-contract "Dynamic Energy" pricing model (analogous in spirit to an interest-rate/utilization controller): it tracks a rolling `energyFactor` that increases when a contract's accumulated `energyUsage` in the previous cycle exceeded a `threshold`, and decays otherwise. Like the PID `_errI` accumulator in the reported bug, the state transition is only evaluated **once per cycle boundary**, using the raw cumulative usage counter, and the counter itself is fully attacker-controlled up to that boundary via ordinary contract calls.

## Finding Description
The energy cost multiplier applied to every opcode of a contract call is `contextContractFactor`, computed in `Program.updateContextContractFactor()`: [1](#0-0) 

This calls `ContractStateCapsule.catchUpToCycle`, which only updates the factor when the currently-observed cycle differs from the last-updated cycle (`lastCycle == newCycle` short-circuits to a no-op): [2](#0-1) 

The decision of whether to *increase* the factor is based solely on `getEnergyUsage() > threshold`, where `energyUsage` is a simple running counter incremented by every call to the contract via `Program.addContextContractUsage`: [3](#0-2) [4](#0-3) 

Because `energyUsage` accumulates freely within a cycle and is only compared against `threshold` at the moment the cycle number changes, a caller can:
1. Send many high-energy calls to a target contract late in a cycle to push `energyUsage` above `threshold`, guaranteeing the factor increase fires exactly once at the next cycle transition, inflating the cost of subsequent calls to that contract for the following cycle(s) (used to grief competitors/dApp users by making their contract artificially "expensive").
2. Conversely, avoid crossing the threshold in a given cycle window (by not calling, or by staying below it) to keep the factor low/decreasing even while heavily using the contract in bursts that straddle cycle boundaries, undermining the mechanism's intended throttling of high-usage contracts.

This mirrors the root cause described in the external report: an economically significant, state-machine-driving quantity (utilization / cumulative usage) is derived from values that a transaction sender fully controls in real time, and the state machine only "locks in" its effect at fixed intervals, allowing manipulation of the direction/magnitude of the change around those boundaries.

## Impact Explanation
This mechanism directly controls TVM energy cost multipliers (`contextContractFactor` in `VM.play`), which changes how much energy (and therefore TRX, since energy purchased above frozen limits is paid for) a caller must pay to interact with a contract: [5](#0-4) 

An attacker who can predictably manipulate whether/when the factor increases can (a) deliberately inflate the cost paid by legitimate users interacting with a target contract in a following cycle (a form of griefing/DoS on gas costs, potentially causing unexpected/increased fee payments or transaction failures for victims budgeting energy), or (b) keep a heavily-used contract's factor artificially low, defeating the anti-congestion purpose of the dynamic energy feature. This is a medium-severity economic/griefing issue rather than a direct fund-theft bug, consistent with the "Medium Risk" classification of the original report.

## Likelihood Explanation
Reaching this path only requires the feature to be enabled (`allowDynamicEnergy`) and sending ordinary TVM contract-call transactions — no special privileges are needed. Any unprivileged transaction sender can trigger `updateContextContractFactor`/`addContextContractUsage` by simply invoking a contract; timing calls around known cycle boundaries (`getCurrentCycleNumber()`, driven by the maintenance schedule) is straightforward and fully within an ordinary user's control.

## Recommendation
Avoid gating the factor transition purely on a freely-inflatable running counter sampled at arbitrary points controlled by the caller. Consider either (a) using a time/usage-weighted average sampled independent of caller-controlled call timing, or (b) applying the increase/decrease evaluation deterministically at well-defined block/maintenance boundaries computed from chain state only (already partially done via `currentCycleNumber`), while ensuring usage cannot be selectively front-loaded within a single cycle to deterministically trigger (or avoid) the transition. This should mirror the general fix recommended in the original report: base the decision on stabilized/previous-cycle values rather than the just-accumulated, attacker-influenced current value at the moment of the boundary crossing.

## Proof of Concept
Conceptual sequence (values illustrative):
1. Dynamic Energy is enabled for a target contract `C` (`allowDynamicEnergy=1`, `dynamicEnergyThreshold=T`).
2. Near the end of cycle `N`, attacker sends several calls to `C`, each adding to `C`'s `ContractStateCapsule.energyUsage` via `Program.addContextContractUsage`, pushing cumulative usage above `T`.
3. On the first call to `C` in cycle `N+1`, `Program.updateContextContractFactor()` invokes `catchUpToCycle`, which sees `getEnergyUsage() > threshold` and increases `energyFactor` for cycle `N+1` — even though the attacker's own calls, not organic usage, caused the trigger.
4. All subsequent callers of `C` in cycle `N+1` now pay elevated `contextContractFactor` energy costs, regardless of their own usage pattern, until the factor decays over following cycles.
5. Repeating this "spike near boundary, then quiet" pattern lets an attacker keep a target contract's cost inflated indefinitely at a fraction of the energy cost a genuinely sustained high-usage pattern would require, or, in the opposite calibration, avoid ever tripping the threshold despite heavy sustained use, defeating the throttle.

Note: I was not able to execute/trace this end-to-end in a live node from the index alone (no access to run the described sequence); the analysis is based on tracing `Program.updateContextContractFactor`/`addContextContractUsage` and `ContractStateCapsule.catchUpToCycle` logic directly from source. A Devin session with repo/test execution access could confirm the exact economic magnitude via `ContractStateCapsuleTest`/`CommitteeConfigTest`.

### Citations

**File:** actuator/src/main/java/org/tron/core/vm/program/Program.java (L2371-2394)
```java
  public long updateContextContractFactor() {
    ContractStateCapsule contractStateCapsule =
        contractState.getContractState(getContextAddress());

    if (contractStateCapsule == null) {
      contractStateCapsule = new ContractStateCapsule(
          contractState.getDynamicPropertiesStore().getCurrentCycleNumber());
      contractState.updateContractState(getContextAddress(), contractStateCapsule);
    } else {
      if (contractStateCapsule.catchUpToCycle(
          contractState.getDynamicPropertiesStore().getCurrentCycleNumber(),
          VMConfig.getDynamicEnergyThreshold(),
          VMConfig.getDynamicEnergyIncreaseFactor(),
          VMConfig.getDynamicEnergyMaxFactor(),
          VMConfig.allowStrictMath(),
          VMConfig.disableJavaLangMath())) {
        contractState.updateContractState(getContextAddress(), contractStateCapsule
        );
      }
    }
    contextContractFactor = contractStateCapsule.getEnergyFactor()
        + Constant.DYNAMIC_ENERGY_FACTOR_DECIMAL;
    return contextContractFactor;
  }
```

**File:** actuator/src/main/java/org/tron/core/vm/program/Program.java (L2396-2402)
```java
  public void addContextContractUsage(long value) {
    ContractStateCapsule contractStateCapsule =
        contractState.getContractState(getContextAddress());

    contractStateCapsule.addEnergyUsage(value);
    contractState.updateContractState(getContextAddress(), contractStateCapsule);
  }
```

**File:** chainbase/src/main/java/org/tron/core/capsule/ContractStateCapsule.java (L54-56)
```java
  public void addEnergyUsage(long toAdd) {
    setEnergyUsage(getEnergyUsage() + toAdd);
  }
```

**File:** chainbase/src/main/java/org/tron/core/capsule/ContractStateCapsule.java (L89-120)
```java
  public boolean catchUpToCycle(
      long newCycle, long threshold, long increaseFactor, long maxFactor,
      boolean useStrictMath, boolean disableMath
  ) {
    long lastCycle = getUpdateCycle();

    // Updated within this cycle
    if (lastCycle == newCycle) {
      return false;
    }

    // Guard judge and uninitialized state
    if (lastCycle > newCycle || lastCycle == 0L) {
      reset(newCycle);
      return true;
    }

    final long precisionFactor = DYNAMIC_ENERGY_FACTOR_DECIMAL;

    // Increase the last cycle
    // fix the threshold = 0 caused incompatible
    if (getEnergyUsage() > threshold) {
      lastCycle += 1;
      double increasePercent = 1 + (double) increaseFactor / precisionFactor;
      this.contractState = ContractState.newBuilder()
          .setUpdateCycle(lastCycle)
          .setEnergyFactor(min(
              maxFactor,
              (long) ((getEnergyFactor() + precisionFactor) * increasePercent) - precisionFactor,
              disableMath))
          .build();
    }
```

**File:** actuator/src/main/java/org/tron/core/vm/VM.java (L23-83)
```java
    try {
      long factor = DYNAMIC_ENERGY_FACTOR_DECIMAL;
      long energyUsage = 0L;
      // hoist once per execution: avoids a per-opcode VMConfig.current() thread-local lookup
      final boolean allowDynamicEnergy = VMConfig.allowDynamicEnergy();

      if (allowDynamicEnergy) {
        factor = program.updateContextContractFactor();
      }

      while (!program.isStopped()) {
        if (VMConfig.vmTrace()) {
          program.saveOpTrace();
        }

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
```
