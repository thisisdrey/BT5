### Title
Single unprivileged caller can indefinitely keep a contract's Dynamic Energy penalty factor at maximum, imposing an escalating energy-cost DoS on all other callers - (File: `actuator/src/main/java/org/tron/core/vm/program/Program.java`)

### Summary
TRON's Dynamic Energy feature multiplies the energy cost of every opcode executed against a contract once that contract's aggregate energy usage in a maintenance cycle exceeds a global threshold. The multiplier ("factor") is stored per-contract in `ContractStateCapsule` and only decays gradually across multiple cycles if usage stays under threshold. Because the threshold check is a simple "did *any* usage in this cycle exceed X" test with no resistance to a single actor's contribution, one unprivileged account can send one minimal, self-funded transaction per maintenance cycle that pushes the contract's cycle usage over the threshold, keeping the penalty factor pinned near its maximum indefinitely. This is structurally the same class of bug as the PoolTogether finding: a cheap, repeatable single-party action perpetuates an elevated/"active" protocol state that should only persist when broad, organic demand justifies it, degrading the experience (here: energy cost) for every other unprivileged user of that contract.

### Finding Description
The per-opcode energy multiplier is fetched once per transaction via `Program.updateContextContractFactor()`: [1](#0-0) 

This delegates to `ContractStateCapsule.catchUpToCycle`, which stores per-contract `energyUsage`, `energyFactor`, and `updateCycle`, keyed by `contractState.getContractState(getContextAddress())`: [2](#0-1) 

The increase condition is simply `getEnergyUsage() > threshold` for the whole prior cycle, without any per-account attribution or resistance to a single caller dominating that usage: [3](#0-2) 

The factor, once penalizing, decays only geometrically over subsequent cycles that stay under threshold (`decreasePercent` computed from `cycleCount`), meaning several consecutive "quiet" cycles are needed to fully reset it: [4](#0-3) 

The resulting factor is applied as a real energy penalty to every opcode executed by every caller of that contract in `VM.play()`: [5](#0-4) 

`addContextContractUsage` then accumulates the current transaction's usage into the shared per-contract counter that feeds next cycle's threshold check: [6](#0-5) 

The cycle boundary is the DPoS maintenance cycle (`dynamicPropertiesStore.getCurrentCycleNumber()`), which is only advanced once per maintenance round (default 6 hours) in `MaintenanceManager.doMaintenance()`: [7](#0-6) 

The threshold/increase/max-factor parameters (`DYNAMIC_ENERGY_THRESHOLD`, `DYNAMIC_ENERGY_INCREASE_FACTOR`, `DYNAMIC_ENERGY_MAX_FACTOR`) are chain-wide committee parameters, not something that adapts per-contract traffic distribution or excludes a single repeat caller: [8](#0-7) 

Because the "was the threshold exceeded" signal is purely a sum over the whole cycle with no minimum-participant / minimum-diversity requirement, exactly like PoolTogether's `largestTierClaimed` being settable by a single claim, one address can single-handedly:
1. Send one transaction to a target contract each maintenance cycle whose energy usage exceeds `DYNAMIC_ENERGY_THRESHOLD`.
2. This increases (or maintains, once at max) `energyFactor` for that contract for the *next* cycle.
3. All other unprivileged callers of that contract then pay energy costs multiplied by up to `DYNAMIC_ENERGY_MAX_FACTOR_RANGE`/`DYNAMIC_ENERGY_FACTOR_DECIMAL` for every opcode, for the entire following cycle, and the attacker repeats before the multi-cycle decay can bring it back down.

### Impact Explanation
An unprivileged attacker can perpetually keep a targeted contract's dynamic-energy multiplier pinned near its maximum by paying for one cheap, minimal-exceedance transaction per maintenance cycle, while every other caller of that same contract (which could be a shared DeFi/vesting/claim contract) is forced to pay grossly inflated energy fees for the same operations, or has transactions revert due to hitting `feeLimit`/energy limits. If the targeted contract has time-bounded operations (e.g., claim/withdraw windows), sustained artificial energy inflation can cause legitimate users to be unable to execute calls economically within those windows, functionally freezing their access to contract-held funds — the same end-state (DoS causing loss/inaccessibility of funds for ordinary users while a single actor perpetuates the adverse condition cheaply) that the PoolTogether report was scored Medium for.

### Likelihood Explanation
This requires only a single unprivileged externally owned account able to send TVM transactions and no special privileges, keys, or SR/witness/consensus roles — it is triggerable purely through ordinary smart contract calls. It does, however, require `ALLOW_DYNAMIC_ENERGY` to be enabled by committee proposal and a contract whose owner has opted the feature threshold/economics into a state exploitable at low cost, so likelihood depends on the specific threshold/factor parameters chosen governance-side and the targeted contract's typical traffic profile.

### Recommendation
Change the trigger condition for increasing/maintaining `energyFactor` so it cannot be dominated by a single caller: e.g., require the excess-usage condition to be satisfied by usage attributable to a minimum number of distinct callers/addresses within the cycle, or use a caller-weighted / capped-per-address contribution when accumulating `energyUsage` toward the threshold, similar in spirit to how PoolTogether hardened `largestTierClaimed` against single-claim manipulation.

### Proof of Concept
1. Committee enables `ALLOW_DYNAMIC_ENERGY` and sets `DYNAMIC_ENERGY_THRESHOLD` to a value representative of normal per-cycle usage for a target contract `C`.
2. Attacker `A` (any funded account) calls `C` once per maintenance cycle with a transaction engineered (e.g., loops, storage writes) so that `C`'s aggregate `energyUsage` for that cycle exceeds `DYNAMIC_ENERGY_THRESHOLD`, as tracked in `ContractStateCapsule.addEnergyUsage` (`actuator/.../Program.java:2396-2402`).
3. On the next maintenance cycle boundary (`MaintenanceManager.doMaintenance`), the first call into `C` triggers `ContractStateCapsule.catchUpToCycle` (`chainbase/.../ContractStateCapsule.java:89-120`), which increases `energyFactor` toward `DYNAMIC_ENERGY_MAX_FACTOR`.
4. For the duration of that cycle, `VM.play()` applies the elevated `factor` as a real energy penalty (`actuator/.../VM.java:52-83`) to every other unprivileged caller of `C`.
5. `A` repeats step 2 every cycle at minimal, predictable cost, preventing the multi-cycle decay in `ContractStateCapsule` from ever bringing `energyFactor` back down, thereby perpetually penalizing all other users of `C`.

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

**File:** chainbase/src/main/java/org/tron/core/capsule/ContractStateCapsule.java (L89-147)
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

    return true;
  }
```

**File:** actuator/src/main/java/org/tron/core/vm/VM.java (L52-83)
```java
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

**File:** consensus/src/main/java/org/tron/consensus/dpos/MaintenanceManager.java (L154-162)
```java
    if (dynamicPropertiesStore.allowChangeDelegation()) {
      long nextCycle = dynamicPropertiesStore.getCurrentCycleNumber() + 1;
      dynamicPropertiesStore.saveCurrentCycleNumber(nextCycle);
      consensusDelegate.getAllWitnesses().forEach(witness -> {
        delegationStore.setBrokerage(nextCycle, witness.createDbKey(),
            delegationStore.getBrokerage(witness.createDbKey()));
        delegationStore.setWitnessVote(nextCycle, witness.createDbKey(), witness.getVoteCount());
      });
    }
```

**File:** actuator/src/main/java/org/tron/core/utils/ProposalUtil.java (L649-689)
```java
      case DYNAMIC_ENERGY_THRESHOLD: {
        if (!forkController.pass(ForkBlockVersionEnum.VERSION_4_7)) {
          throw new ContractValidateException(
              "Bad chain parameter id [DYNAMIC_ENERGY_THRESHOLD]");
        }

        if (value < 0 || value > LONG_VALUE) {
          throw new ContractValidateException(LONG_VALUE_ERROR);
        }
        break;
      }
      case DYNAMIC_ENERGY_INCREASE_FACTOR: {
        if (!forkController.pass(ForkBlockVersionEnum.VERSION_4_7)) {
          throw new ContractValidateException(
              "Bad chain parameter id [DYNAMIC_ENERGY_INCREASE_FACTOR]");
        }

        if (value < 0 || value > DYNAMIC_ENERGY_INCREASE_FACTOR_RANGE) {
          throw new ContractValidateException(
              "This value[DYNAMIC_ENERGY_INCREASE_FACTOR] "
                  + "is only allowed to be in the range 0-"
                  + DYNAMIC_ENERGY_INCREASE_FACTOR_RANGE
          );
        }
        break;
      }
      case DYNAMIC_ENERGY_MAX_FACTOR: {
        if (!forkController.pass(ForkBlockVersionEnum.VERSION_4_7)) {
          throw new ContractValidateException(
              "Bad chain parameter id [DYNAMIC_ENERGY_MAX_FACTOR]");
        }

        if (value < 0 || value > DYNAMIC_ENERGY_MAX_FACTOR_RANGE) {
          throw new ContractValidateException(
              "This value[DYNAMIC_ENERGY_MAX_FACTOR] "
                  + "is only allowed to be in the range 0-"
                  + DYNAMIC_ENERGY_MAX_FACTOR_RANGE
          );
        }
        break;
      }
```
