### Title
Dynamic energy factor recalculation in `ContractStateCapsule.catchUpToCycle` produces call-cadence-dependent energy pricing - ([File: chainbase/src/main/java/org/tron/core/capsule/ContractStateCapsule.java])

### Summary
The dynamic-energy pricing mechanism for TVM contract calls mixes a single-step, non-compounding "increase" with a multi-cycle compounding "decrease," so the resulting `energyFactor` (and hence the ENERGY cost charged for calling a contract) depends on how often the contract is invoked across cycles rather than solely on the actual demand/usage history — the same class of call-frequency-dependent miscalculation described in the external UToken report.

### Finding Description
`ContractStateCapsule.catchUpToCycle` [1](#0-0)  updates a per-contract `energyFactor` used to scale ENERGY cost when a contract is "hot" (dynamic energy). When the cycle catch-up happens, the function applies **at most one** non-compounding increase step regardless of how many cycles have actually passed with usage above `threshold`: [2](#0-1) 

It then compounds a **decrease** using `pow(decreasePercent, cycleCount)` for the remaining cycle gap: [3](#0-2) 

This mirrors exactly the reported bug class: one branch of the recurrence is a single "simple" step applied once per invocation of the catch-up function, while the other branch is a true compounding function over however many cycles were skipped. Consequently:
- If `catchUpToCycle` is invoked every single cycle (e.g., a contract called at least once per cycle keeping `getEnergyUsage()` above `threshold` each time), the increase step is applied **once per cycle**, compounding naturally over time.
- If `catchUpToCycle` is invoked sporadically after several cycles are skipped (e.g., a contract goes idle for multiple cycles, or a caller strategically avoids triggering catch-up during those cycles), only **one** increase step is ever applied for that gap, and the rest of the elapsed cycles are folded into the decrease-only `pow()` branch.

This produces two different final `energyFactor` values for the same real-world energy-usage history, purely as a function of how often the state-catch-up path is triggered — the analog of `simpleInterestFactor` vs. per-block compounding in the reported UToken issue.

The catch-up is driven from `Program.updateContextContractFactor()` in the TVM execution path, which is invoked on ordinary contract calls: [4](#0-3) , meaning any transaction broadcaster who can call a contract can influence when/whether the catch-up executes and therefore the trajectory of the resulting factor.

### Impact Explanation
The `energyFactor` directly scales the ENERGY price multiplier applied to callers of a "hot" contract under the dynamic-energy mechanism. Because its final value is dependent on call cadence rather than a consistent function of elapsed time and usage, a party who controls the calling pattern of a contract (e.g., timing calls to straddle multiple idle cycles versus calling every cycle) can cause the network to compute a materially different (generally lower) energy price than an honest, cycle-consistent execution would produce. This is a resource-pricing/energy-metering correctness issue: it does not directly move TRX or provide an unauthorized privileged operation, and its ultimate effect is confined to energy fee accounting for contract execution.

### Likelihood Explanation
Reachable via ordinary, unprivileged TVM contract invocation — any address can call a contract enough times, or deliberately skip cycles, to shape when `catchUpToCycle` fires and thus which code branch (single-step increase vs. compounding decrease) is exercised. No special privilege or malicious-SR/witness/peer role is required.

### Recommendation
Make the increase branch cycle-count aware and consistent with the decrease branch (i.e., compound the increase over the actual number of over-threshold cycles rather than applying it once per catch-up call), so the resulting `energyFactor` depends only on elapsed cycles and recorded usage, not on how often `catchUpToCycle` happens to be invoked.

### Proof of Concept
Not independently verified against a live network; based on static analysis of the recurrence in `ContractStateCapsule.catchUpToCycle`. To confirm impact, a Devin agent with test-harness access should:
1. Simulate calling a contract every cycle with usage above `threshold` for N cycles and record the resulting `energyFactor`.
2. Simulate the same total usage/timing but let the contract go idle so `catchUpToCycle` is invoked only once after N cycles, and compare the resulting `energyFactor`.
3. Confirm the two values diverge for identical total elapsed cycles and usage profile, demonstrating cadence-dependence as in `framework/src/test/java/org/tron/core/capsule/ContractStateCapsuleTest.java` (existing unit tests reference `catchUpToCycle`/`EnergyFactor` and could be extended for this comparison) [5](#0-4) .

### Citations

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

**File:** chainbase/src/main/java/org/tron/core/capsule/ContractStateCapsule.java (L122-144)
```java
    // No need to decrease
    long cycleCount = newCycle - lastCycle;
    if (cycleCount <= 0) {
      return true;
    }

    // Calc the decrease percent (decrease factor [75% ~ 100%])
    double decreasePercent = pow(
        1 - (double) increaseFactor / DYNAMIC_ENERGY_DECREASE_DIVISION / precisionFactor,
        cycleCount, useStrictMath
    );

    // Decrease to this cycle
    // (If long time no tx and factor is 100%,
    //  we just calc it again and result factor is still 100%.
    //  That means we merge this special case to normal cases)
    this.contractState = ContractState.newBuilder()
        .setUpdateCycle(newCycle)
        .setEnergyFactor(max(
            0,
            (long) ((getEnergyFactor() + precisionFactor) * decreasePercent) - precisionFactor,
            disableMath))
        .build();
```

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

**File:** framework/src/test/java/org/tron/core/capsule/ContractStateCapsuleTest.java (L1-1)
```java
package org.tron.core.capsule;
```
