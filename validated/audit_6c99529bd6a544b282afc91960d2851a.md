## Title
Free, instant manipulation of `TOTAL_NET_WEIGHT`/`TOTAL_ENERGY_WEIGHT` via unbonded Freeze V2/Unfreeze V2 dilutes other accounts' resource limits - (File: `actuator/src/main/java/org/tron/core/actuator/FreezeBalanceV2Actuator.java`, `actuator/src/main/java/org/tron/core/actuator/UnfreezeBalanceV2Actuator.java`)

### Summary
The Aloe report describes a shared risk parameter (pool liquidity) that can be inflated for free within a single block by depositing then withdrawing, which distorts a ratio-based calculation (IV) used by every other position. java-tron's StakeV2 resource model has an analogous pattern: `TOTAL_NET_WEIGHT`/`TOTAL_ENERGY_WEIGHT` is a global denominator shared by every staker's bandwidth/energy limit, and `FreezeBalanceV2Contract`/`UnfreezeBalanceV2Contract` allow any account to inflate then immediately deflate this shared denominator at zero cost (no lock-up), unlike the legacy Freeze V1 flow which enforced a minimum freeze duration before unfreeze was permitted.

### Finding Description
`calculateGlobalEnergyLimitV2`/`calculateGlobalLimitV2` compute an account's resource limit as `frozenBalance * totalLimit / totalWeight`, where `totalWeight` (`TOTAL_ENERGY_WEIGHT`/`TOTAL_NET_WEIGHT`) is a chain-wide value shared by all stakers: [1](#0-0) 

That weight is mutated instantly and unconditionally by `FreezeBalanceV2Actuator.execute()`: [2](#0-1) 

and by `UnfreezeBalanceV2Actuator.execute()`/`updateTotalResourceWeight()`, which decrements it right away regardless of whether the underlying TRX has actually been returned to the account (return of principal is delayed by `unfreezeDelayDays`, but the weight change is immediate): [3](#0-2) 

Crucially, `UnfreezeBalanceV2Actuator.validate()` imposes **no minimum time-frozen requirement** before an account can call unfreeze — it only checks that a positive frozen amount exists (`checkExistFrozenBalance`/`checkUnfreezeBalance`): [4](#0-3) 

This is a deliberate behavioral change from the legacy V1 flow, where `FreezeBalanceActuator.validate()` enforced `frozenDuration >= minFrozenTime` and `UnfreezeBalanceActuator` checked `expireTime <= now` before permitting unfreeze, i.e., capital had to remain locked (opportunity cost) for a minimum period: [5](#0-4) [6](#0-5) 

With Freeze V2, an attacker can broadcast `FreezeBalanceV2Contract` (temporarily inflating `TOTAL_ENERGY_WEIGHT`/`TOTAL_NET_WEIGHT` and their own frozen balance) and, within the same or the very next block, broadcast `UnfreezeBalanceV2Contract` to instantly revert the weight change — exactly mirroring the "deposit, update, withdraw" free-manipulation pattern in the Aloe report, except here the manipulated value is the shared resource-limit denominator instead of an IV input.

### Impact Explanation
While the attacker's own frozen balance moves in lockstep with the total weight (so they don't directly gain a lasting advantage), the shared denominator briefly changes for every other account computing `calculateGlobalEnergyLimitV2`/`calculateGlobalNetLimitV2` during the window between the freeze and unfreeze transactions. Because resource limits are recomputed live from `TOTAL_ENERGY_WEIGHT`/`TOTAL_NET_WEIGHT` on every usage check, an attacker can transiently dilute other accounts' available bandwidth/energy within a block window they control, at the cost of gas only (no locked capital, since Freeze V2 has no minimum hold time before unfreeze). This can cause a victim's transaction to be treated as exceeding its energy/bandwidth allowance and be charged TRX fees (or fail) that it otherwise would not have incurred — a griefing/fee-manipulation vector against arbitrary accounts, reachable by any unprivileged transaction broadcaster.

### Likelihood Explanation
Any account holder can trigger this with two ordinary system contracts (`FreezeBalanceV2Contract`, `UnfreezeBalanceV2Contract`) that require no special privilege — only enough TRX balance to freeze (which is never actually locked, since there is no minimum freeze duration check, and the coins are returned after `unfreezeDelayDays` while the weight change itself is immediate). The absence of any minimum-hold check in `UnfreezeBalanceV2Actuator.validate()` makes the manipulation trivially and repeatedly executable at will.

### Recommendation
Reintroduce a minimum freeze duration (mirroring `minFrozenTime` in the legacy `FreezeBalanceActuator`) before `UnfreezeBalanceV2Contract` is permitted to reduce `TOTAL_NET_WEIGHT`/`TOTAL_ENERGY_WEIGHT` for a given freeze, or compute resource limits from a time-weighted/averaged `TOTAL_NET_WEIGHT`/`TOTAL_ENERGY_WEIGHT` rather than the instantaneous value, so that single-block freeze/unfreeze pairs cannot manipulate other accounts' resource limits.

### Proof of Concept
1. Attacker account A calls `FreezeBalanceV2Contract` with a large `frozenBalance` for `ENERGY`, which calls `dynamicStore.addTotalEnergyWeight(...)` and inflates `TOTAL_ENERGY_WEIGHT` (`FreezeBalanceV2Actuator.java:57-72`).
2. In the same block, victim account B's transaction is processed; its available energy is computed via `calculateGlobalEnergyLimitV2` using the now-inflated `TOTAL_ENERGY_WEIGHT` denominator (`EnergyProcessor.java:168-179`), reducing B's proportional share and potentially causing B to run out of energy and be billed TRX for the shortfall.
3. Attacker A immediately calls `UnfreezeBalanceV2Contract` for the same amount; `UnfreezeBalanceV2Actuator.validate()` allows this instantly since there is no minimum-hold check (`UnfreezeBalanceV2Actuator.java:145-182`), and `updateTotalResourceWeight` reverts `TOTAL_ENERGY_WEIGHT` back to its original value (`UnfreezeBalanceV2Actuator.java:274-301`).
4. A's capital was only exposed for the duration of steps 1–3 (a fraction of a block), at the cost of gas only, while B's resource limit was manipulated during that window.

### Citations

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

**File:** actuator/src/main/java/org/tron/core/actuator/FreezeBalanceV2Actuator.java (L57-72)
```java
    long frozenBalance = freezeBalanceV2Contract.getFrozenBalance();
    long newBalance = accountCapsule.getBalance() - frozenBalance;

    switch (freezeBalanceV2Contract.getResource()) {
      case BANDWIDTH:
        long oldNetWeight = accountCapsule.getFrozenV2BalanceWithDelegated(BANDWIDTH) / TRX_PRECISION;
        accountCapsule.addFrozenBalanceForBandwidthV2(frozenBalance);
        long newNetWeight = accountCapsule.getFrozenV2BalanceWithDelegated(BANDWIDTH) / TRX_PRECISION;
        dynamicStore.addTotalNetWeight(newNetWeight - oldNetWeight);
        break;
      case ENERGY:
        long oldEnergyWeight = accountCapsule.getFrozenV2BalanceWithDelegated(ENERGY) / TRX_PRECISION;
        accountCapsule.addFrozenBalanceForEnergyV2(frozenBalance);
        long newEnergyWeight = accountCapsule.getFrozenV2BalanceWithDelegated(ENERGY) / TRX_PRECISION;
        dynamicStore.addTotalEnergyWeight(newEnergyWeight - oldEnergyWeight);
        break;
```

**File:** actuator/src/main/java/org/tron/core/actuator/UnfreezeBalanceV2Actuator.java (L145-182)
```java
    switch (unfreezeBalanceV2Contract.getResource()) {
      case BANDWIDTH:
        if (!checkExistFrozenBalance(accountCapsule, BANDWIDTH)) {
          throw new ContractValidateException("no frozenBalance(BANDWIDTH)");
        }
        break;
      case ENERGY:
        if (!checkExistFrozenBalance(accountCapsule, ENERGY)) {
          throw new ContractValidateException("no frozenBalance(Energy)");
        }
        break;
      case TRON_POWER:
        if (dynamicStore.supportAllowNewResourceModel()) {
          if (!checkExistFrozenBalance(accountCapsule, TRON_POWER)) {
            throw new ContractValidateException("no frozenBalance(TronPower)");
          }
        } else {
          throw new ContractValidateException("ResourceCode error.valid ResourceCode[BANDWIDTH、Energy]");
        }
        break;
      default:
        if (dynamicStore.supportAllowNewResourceModel()) {
          throw new ContractValidateException("ResourceCode error.valid ResourceCode[BANDWIDTH、Energy、TRON_POWER]");
        } else {
          throw new ContractValidateException("ResourceCode error.valid ResourceCode[BANDWIDTH、Energy]");
        }
    }

    if (!checkUnfreezeBalance(accountCapsule, unfreezeBalanceV2Contract, unfreezeBalanceV2Contract.getResource())) {
      throw new ContractValidateException(
          "Invalid unfreeze_balance, [" + unfreezeBalanceV2Contract.getUnfreezeBalance() + "] is error"
      );
    }

    int unfreezingCount = accountCapsule.getUnfreezingV2Count(now);
    if (UNFREEZE_MAX_TIMES <= unfreezingCount) {
      throw new ContractValidateException("Invalid unfreeze operation, unfreezing times is over limit");
    }
```

**File:** actuator/src/main/java/org/tron/core/actuator/UnfreezeBalanceV2Actuator.java (L274-301)
```java
  public void updateTotalResourceWeight(AccountCapsule accountCapsule,
                                        final UnfreezeBalanceV2Contract unfreezeBalanceV2Contract,
                                        long unfreezeBalance) {
    DynamicPropertiesStore dynamicStore = chainBaseManager.getDynamicPropertiesStore();
    switch (unfreezeBalanceV2Contract.getResource()) {
      case BANDWIDTH:
        long oldNetWeight = accountCapsule.getFrozenV2BalanceWithDelegated(BANDWIDTH) / TRX_PRECISION;
        accountCapsule.addFrozenBalanceForBandwidthV2(-unfreezeBalance);
        long newNetWeight = accountCapsule.getFrozenV2BalanceWithDelegated(BANDWIDTH) / TRX_PRECISION;
        dynamicStore.addTotalNetWeight(newNetWeight - oldNetWeight);
        break;
      case ENERGY:
        long oldEnergyWeight = accountCapsule.getFrozenV2BalanceWithDelegated(ENERGY) / TRX_PRECISION;
        accountCapsule.addFrozenBalanceForEnergyV2(-unfreezeBalance);
        long newEnergyWeight = accountCapsule.getFrozenV2BalanceWithDelegated(ENERGY) / TRX_PRECISION;
        dynamicStore.addTotalEnergyWeight(newEnergyWeight - oldEnergyWeight);
        break;
      case TRON_POWER:
        long oldTPWeight = accountCapsule.getTronPowerFrozenV2Balance() / TRX_PRECISION;
        accountCapsule.addFrozenForTronPowerV2(-unfreezeBalance);
        long newTPWeight = accountCapsule.getTronPowerFrozenV2Balance() / TRX_PRECISION;
        dynamicStore.addTotalTronPowerWeight(newTPWeight - oldTPWeight);
        break;
      default:
        //this should never happen
        break;
    }
  }
```

**File:** actuator/src/main/java/org/tron/core/actuator/FreezeBalanceActuator.java (L203-214)
```java
    long frozenDuration = freezeBalanceContract.getFrozenDuration();
    long minFrozenTime = dynamicStore.getMinFrozenTime();
    long maxFrozenTime = dynamicStore.getMaxFrozenTime();

    boolean needCheckFrozeTime = CommonParameter.getInstance()
        .getCheckFrozenTime() == 1;//for test
    if (needCheckFrozeTime && !(frozenDuration >= minFrozenTime
        && frozenDuration <= maxFrozenTime)) {
      throw new ContractValidateException(
          "frozenDuration must be less than " + maxFrozenTime + " days "
              + "and more than " + minFrozenTime + " days");
    }
```

**File:** actuator/src/main/java/org/tron/core/actuator/UnfreezeBalanceActuator.java (L442-456)
```java
          long allowedUnfreezeCount = accountCapsule.getFrozenList().stream()
              .filter(frozen -> frozen.getExpireTime() <= now).count();
          if (allowedUnfreezeCount <= 0) {
            throw new ContractValidateException("It's not time to unfreeze(BANDWIDTH).");
          }
          break;
        case ENERGY:
          Frozen frozenBalanceForEnergy = accountCapsule.getAccountResource()
              .getFrozenBalanceForEnergy();
          if (frozenBalanceForEnergy.getFrozenBalance() <= 0) {
            throw new ContractValidateException("no frozenBalance(Energy)");
          }
          if (frozenBalanceForEnergy.getExpireTime() > now) {
            throw new ContractValidateException("It's not time to unfreeze(Energy).");
          }
```
