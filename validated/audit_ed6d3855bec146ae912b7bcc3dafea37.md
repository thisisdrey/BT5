### Title
Node crash via unguarded division-by-zero in `EnergyProcessor.calculateGlobalEnergyLimit` when `ALLOW_HARDEN_RESOURCE_CALCULATION` is combined with a zero/negative `TOTAL_ENERGY_WEIGHT` on the legacy (pre-`supportUnfreezeDelay`) freeze path - ([File: chainbase/src/main/java/org/tron/core/db/EnergyProcessor.java])

### Summary
The external report describes BIND crashing only when two independently-configurable features (QNAME minimization + forward-first) are combined, producing a code path the developers never defended against. The java-tron analog is `EnergyProcessor.calculateGlobalEnergyLimit`, which only guards against a zero total-energy-weight when the `allowNewReward` proposal is active; otherwise it falls through to an `assert` (a no-op in production, since Java assertions are disabled by default) and then, if `ALLOW_HARDEN_RESOURCE_CALCULATION` is also enabled, performs an unguarded `BigInteger` division by `totalEnergyWeight`, which throws `ArithmeticException: BigInteger divide by zero` when that value is `0`.

### Finding Description
`calculateGlobalEnergyLimit` reads `totalEnergyWeight` and only returns early (`return 0`) when `dynamicPropertiesStore.allowNewReward()` is true and the weight is `<= 0`: [1](#0-0) 

If `allowNewReward` is not active (older/legacy chain configuration, or a network that has not yet enacted that proposal) the code falls into the `else` branch containing only `assert totalEnergyWeight > 0;` — which does nothing at runtime because JVMs run with assertions disabled unless `-ea` is explicitly passed. Execution then continues into `hardenCalculation()`, which — when `ALLOW_HARDEN_RESOURCE_CALCULATION` (a separately, later-activated proposal) is enabled — calls `calculateGlobalLimitV1`: [2](#0-1) 

`calculateGlobalLimitV1` performs `BigInteger.valueOf(weight).multiply(...).divide(BigInteger.valueOf(totalWeight))`. When `totalWeight == 0`, `BigInteger.divide` throws an unchecked `ArithmeticException`, which is not caught anywhere in `EnergyProcessor` or `ResourceProcessor` (no `catch ArithmeticException` exists in that package).

This mirrors the CVE's root cause precisely: the safety check was only wired for one specific combination of options (`allowNewReward`), and a second, independently toggled option (`allowHardenResourceCalculation`) reactivates a previously-dormant crash path that the original single-flag guard never anticipated.

The vulnerable branch is only reached on the legacy freeze-energy accounting path, i.e., when `dynamicPropertiesStore.supportUnfreezeDelay()` is false (the V2 unfreeze-delay path, `calculateGlobalEnergyLimitV2`, does correctly guard `totalEnergyWeight == 0`): [3](#0-2) 

`totalEnergyWeight` is the network-wide sum of legacy frozen-for-energy balances; it becomes `0` if every account that had frozen TRX for energy under the old model unfreezes it (via `UnfreezeBalanceActuator`), which is an unprivileged, broadcastable transaction available to any account holder.

### Impact Explanation
An uncaught `ArithmeticException` thrown deep inside energy accounting during ordinary transaction/contract execution is not handled by any `catch` clause in the resource-accounting classes. This is a `RuntimeException` subtype, distinct from the checked exceptions (`ContractValidateException`, `ContractExeException`, etc.) that `Manager.pushBlock`/`applyBlock`/`processBlock` explicitly declare and handle; it will propagate up as an unexpected runtime error during block application, capable of aborting/halting block processing for the node that hits it (denial of service / node crash), matching the "node crash or halt" impact criterion.

### Likelihood Explanation
Reaching the crash requires: (1) the chain has `ALLOW_HARDEN_RESOURCE_CALCULATION` enacted, (2) `allowNewReward` not enacted, and (3) `supportUnfreezeDelay` (the FreezeV2 unfreeze-delay fork) not enacted, and (4) `totalEnergyWeight` driven to `0` by unfreezing all legacy energy stakes. Because `ALLOW_HARDEN_RESOURCE_CALCULATION` was introduced at a later fork version than `allowNewReward`/`supportUnfreezeDelay` were on most long-lived networks, this exact combination is more plausible on newer test/private networks or early-stage forks that activate the harden-calculation proposal before the reward/unfreeze-delay proposals, or transiently during a coordinated hard-fork rollout window. On mainnet today `supportUnfreezeDelay` is very likely already active, which would route through the safe V2 method — so the practical exposure window is narrower than the CVSS 7.5 network-only prerequisite in the original BIND report, but the code path itself remains present and unguarded.

### Recommendation
Remove the `else { assert totalEnergyWeight > 0; }` reliance on Java assertions in `EnergyProcessor.calculateGlobalEnergyLimit`, and unconditionally short-circuit to `return 0` whenever `totalEnergyWeight <= 0`, regardless of `allowNewReward`'s state — mirroring the guard already present in `calculateGlobalEnergyLimitV2`. Apply the same audit to any other caller of `calculateGlobalLimitV1`/`calculateGlobalLimitV2` and `divideCeilExact`/`getUsage` in `ResourceProcessor` and `RepositoryImpl` that similarly gate zero-denominator protection behind a single proposal flag instead of validating the denominator directly before every `BigInteger.divide` call.

### Proof of Concept
1. Deploy/operate a java-tron network where `ALLOW_HARDEN_RESOURCE_CALCULATION` has been enacted via governance but `ALLOW_NEW_REWARD` (and the FreezeV2 unfreeze-delay fork) has not yet been enacted.
2. Have every account that has ever frozen TRX for ENERGY under the legacy model broadcast `UnfreezeBalanceActuator` transactions until the network's `TOTAL_ENERGY_WEIGHT` dynamic property reaches `0`.
3. Broadcast any `TriggerSmartContract` transaction from any account that still has a non-zero legacy energy freeze (`getAllFrozenBalanceForEnergy() >= TRX_PRECISION`) so that `EnergyProcessor.calculateGlobalEnergyLimit`/`useEnergy` is invoked during transaction execution.
4. `calculateGlobalEnergyLimit` skips the dead `totalEnergyWeight <= 0` return (since `allowNewReward()` is false), falls into `calculateGlobalLimitV1`, and throws `ArithmeticException: BigInteger divide by zero`, which is uncaught in the resource-accounting call stack and propagates during block application.

Note: I could not fully trace every intermediate caller between `useEnergy`/`calculateGlobalEnergyLimit` and the top-level `Manager.pushBlock`/`applyBlock` handlers in the time available (grep showed callers in `ReceiptCapsule.java`, `Program.java`, `ContractState.java`, `Wallet.java`), so the exact set of checked-exception wrappers that the `ArithmeticException` passes through before surfacing was not exhaustively verified; a Devin session with full repo access should trace this call chain end-to-end to confirm precisely where the crash surfaces (per-transaction exception handling that fails the tx, vs. a true JVM-level halt).

### Citations

**File:** chainbase/src/main/java/org/tron/core/db/EnergyProcessor.java (L145-166)
```java
  public long calculateGlobalEnergyLimit(AccountCapsule accountCapsule) {
    long frozeBalance = accountCapsule.getAllFrozenBalanceForEnergy();
    if (dynamicPropertiesStore.supportUnfreezeDelay()) {
      return calculateGlobalEnergyLimitV2(frozeBalance);
    }
    if (frozeBalance < TRX_PRECISION) {
      return 0;
    }

    long totalEnergyLimit = dynamicPropertiesStore.getTotalEnergyCurrentLimit();
    long totalEnergyWeight = dynamicPropertiesStore.getTotalEnergyWeight();
    if (dynamicPropertiesStore.allowNewReward() && totalEnergyWeight <= 0) {
      return 0;
    } else {
      assert totalEnergyWeight > 0;
    }
    if (hardenCalculation()) {
      return calculateGlobalLimitV1(frozeBalance, totalEnergyLimit, totalEnergyWeight);
    }
    long energyWeight = frozeBalance / TRX_PRECISION;
    return (long) (energyWeight * ((double) totalEnergyLimit / totalEnergyWeight));
  }
```

**File:** chainbase/src/main/java/org/tron/core/db/EnergyProcessor.java (L168-179)
```java
  public long calculateGlobalEnergyLimitV2(long frozeBalance) {
    long totalEnergyLimit = dynamicPropertiesStore.getTotalEnergyCurrentLimit();
    long totalEnergyWeight = dynamicPropertiesStore.getTotalEnergyWeight();
    if (totalEnergyWeight == 0) {
      return 0;
    }
    if (hardenCalculation()) {
      return calculateGlobalLimitV2(frozeBalance, totalEnergyLimit, totalEnergyWeight);
    }
    double energyWeight = (double) frozeBalance / TRX_PRECISION;
    return (long) (energyWeight * ((double) totalEnergyLimit / totalEnergyWeight));
  }
```

**File:** chainbase/src/main/java/org/tron/core/db/ResourceProcessor.java (L350-357)
```java
  protected long calculateGlobalLimitV1(long frozeBalance,
      long totalLimit, long totalWeight) {
    long weight = frozeBalance / TRX_PRECISION;
    return BigInteger.valueOf(weight)
        .multiply(BigInteger.valueOf(totalLimit))
        .divide(BigInteger.valueOf(totalWeight))
        .longValueExact();
  }
```
