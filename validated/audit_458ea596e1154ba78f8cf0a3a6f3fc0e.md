### Title
Missing floor-at-zero clamp on global resource weight counters reachable via TVM native contracts - ([File: actuator/src/main/java/org/tron/core/vm/repository/RepositoryImpl.java])

### Summary
`RepositoryImpl.addTotalNetWeight`, `addTotalEnergyWeight`, and `addTotalTronPowerWeight` update the chain-wide `TOTAL_NET_WEIGHT` / `TOTAL_ENERGY_WEIGHT` / `TOTAL_TRON_POWER_WEIGHT` dynamic properties without ever clamping the result to a non-negative floor, unlike the equivalent methods in `DynamicPropertiesStore`, which explicitly clamp with `max(0, totalWeight, ...)`. [1](#0-0) [2](#0-1) 

### Finding Description
Two independent code paths update the same global resource-accounting counters:

1. The direct actuator path (`FreezeBalanceActuator`, `FreezeBalanceV2Actuator`, `UnfreezeBalanceActuator`, `UnfreezeBalanceV2Actuator`) calls `dynamicStore.addTotalNetWeight(...)` / `addTotalEnergyWeight(...)` / `addTotalTronPowerWeight(...)` directly on `DynamicPropertiesStore`, which contains a defensive clamp: when `allowNewReward()` is active, the new value is floored at zero via `max(0, totalWeight, ...)`. [2](#0-1) 

2. The TVM native-contract path — reachable from any smart contract that invokes the `freezeBalanceV2`, `unfreezeBalanceV2`, `cancelAllUnfreezeV2`, `delegateResource`, or `unDelegateResource` precompiles (`FreezeBalanceV2Processor`, `UnfreezeBalanceV2Processor`, `CancelAllUnfreezeV2Processor`, `DelegateResourceProcessor`, `UnDelegateResourceProcessor`) — calls `repo.addTotalNetWeight(...)` / `repo.addTotalEnergyWeight(...)` / `repo.addTotalTronPowerWeight(...)`, which resolve to `RepositoryImpl`'s implementation. That implementation performs the same running `+=` update but has **no** floor-at-zero clamp at all. [1](#0-0) [3](#0-2) [4](#0-3) [5](#0-4) 

This mirrors the root cause of the referenced report: a global aggregate accounting value that is decremented via signed deltas is only sanity-checked (floored at 0) on one of two code paths that mutate it, while the other path performs the raw signed addition unconditionally. In the FlatCoin report, an unguarded cast from a negative signed delta produced an astronomically large unsigned value that later reverted in an invariant check. In java-tron, an unguarded negative delta on `TOTAL_NET_WEIGHT`/`TOTAL_ENERGY_WEIGHT`/`TOTAL_TRON_POWER_WEIGHT` via the TVM-reachable path can drive these long counters negative and leave them uncorrected, since the RepositoryImpl path never applies the clamp that the "trusted" actuator path relies on.

These totals are used as the divisor for computing every account's available bandwidth/energy limit from its frozen balance (`calculateGlobalLimitV1`/`V2` style computations use `totalNetWeight`/`totalEnergyWeight` as the denominator). [6](#0-5) 
A negative (or zero, in the divide-by-total-weight case) global weight corrupts bandwidth/energy limit computation for every account on the network, since this is a chain-wide singleton value, not a per-account one.

### Impact Explanation
`TOTAL_NET_WEIGHT`/`TOTAL_ENERGY_WEIGHT`/`TOTAL_TRON_POWER_WEIGHT` are consensus-critical, chain-wide dynamic properties consumed by resource-limit math for every account's bandwidth/energy allocation. If a value goes negative through the unguarded `RepositoryImpl` path and is never corrected, all subsequent resource calculations that divide by this total become corrupted (wrong result, or divide-by-zero/negative behavior), which is a network-wide denial-of-service style condition rather than a localized per-account issue — consistent with the escalation bucket for stake/delegation/reward math and TVM-reachable native contract accounting.

### Likelihood Explanation
Likelihood is uncertain without full transaction-level reachability confirmation: the delta passed into `repo.addTotalNetWeight`/`addTotalEnergyWeight` in these processors is computed from `newWeight - oldWeight`, both derived from the same account's frozen-balance state, so in the straightforward case increments and decrements should be self-consistent and bounded per operation. Establishing a concrete way for an attacker-controlled contract to force a *net* negative excursion (e.g., through repeated freeze/unfreeze/cancel/self-destruct sequences, rounding truncation from `/ TRX_PRECISION`, or an ordering edge-case) would require deeper testing/fuzzing of the native-contract flows than was possible within the scope of this review. This is flagged as a structural inconsistency (missing defensive clamp on a TVM-reachable path vs. the guarded actuator path) rather than a fully proven end-to-end exploit.

### Recommendation
Apply the same floor-at-zero (or equivalent invariant) clamp used in `DynamicPropertiesStore.addTotalNetWeight`/`addTotalEnergyWeight`/`addTotalTronPowerWeight` inside `RepositoryImpl`'s equivalent methods, so that the TVM native-contract path cannot leave these chain-wide counters in a corrupted (negative) state that differs from the guarantee provided on the direct-actuator path.

### Proof of Concept
Not fully reproduced. A background engineer should write a JUnit test exercising `FreezeBalanceV2Processor` / `UnfreezeBalanceV2Processor` / `CancelAllUnfreezeV2Processor` through repeated freeze/unfreeze/cancel/self-destruct sequences invoked from a TVM contract (mirroring the existing `FreezeV2Test.java` harness) to attempt to drive `dynamicStore.getTotalNetWeight()`/`getTotalEnergyWeight()` negative via `RepositoryImpl.addTotalNetWeight`, and compare against the equivalent actuator-only flow (`FreezeBalanceV2Actuator`/`UnfreezeBalanceV2Actuator`) to confirm the clamp discrepancy is externally observable and persistent. [7](#0-6)

### Citations

**File:** actuator/src/main/java/org/tron/core/vm/repository/RepositoryImpl.java (L1196-1217)
```java
  //The unit is trx
  @Override
  public void addTotalNetWeight(long amount) {
    long totalNetWeight = getTotalNetWeight();
    totalNetWeight += amount;
    saveTotalNetWeight(totalNetWeight);
  }

  //The unit is trx
  @Override
  public void addTotalEnergyWeight(long amount) {
    long totalEnergyWeight = getTotalEnergyWeight();
    totalEnergyWeight += amount;
    saveTotalEnergyWeight(totalEnergyWeight);
  }

  @Override
  public void addTotalTronPowerWeight(long amount) {
    long totalTronPowerWeight = getTotalTronPowerWeight();
    totalTronPowerWeight += amount;
    saveTotalTronPowerWeight(totalTronPowerWeight);
  }
```

**File:** chainbase/src/main/java/org/tron/core/store/DynamicPropertiesStore.java (L2269-2306)
```java
  //The unit is trx
  public void addTotalNetWeight(long amount) {
    if (amount == 0) {
      return;
    }
    long totalNetWeight = getTotalNetWeight();
    totalNetWeight += amount;
    if (allowNewReward()) {
      totalNetWeight = max(0, totalNetWeight, disableJavaLangMath());
    }
    saveTotalNetWeight(totalNetWeight);
  }

  //The unit is trx
  public void addTotalEnergyWeight(long amount) {
    if (amount == 0) {
      return;
    }
    long totalEnergyWeight = getTotalEnergyWeight();
    totalEnergyWeight += amount;
    if (allowNewReward()) {
      totalEnergyWeight = max(0, totalEnergyWeight, disableJavaLangMath());
    }
    saveTotalEnergyWeight(totalEnergyWeight);
  }

  //The unit is trx
  public void addTotalTronPowerWeight(long amount) {
    if (amount == 0) {
      return;
    }
    long totalWeight = getTotalTronPowerWeight();
    totalWeight += amount;
    if (allowNewReward()) {
      totalWeight = max(0, totalWeight, disableJavaLangMath());
    }
    saveTotalTronPowerWeight(totalWeight);
  }
```

**File:** actuator/src/main/java/org/tron/core/vm/nativecontract/FreezeBalanceV2Processor.java (L83-104)
```java
    switch (param.getResourceType()) {
      case BANDWIDTH:
        long oldNetWeight = accountCapsule.getFrozenV2BalanceWithDelegated(BANDWIDTH) / TRX_PRECISION;
        accountCapsule.addFrozenBalanceForBandwidthV2(frozenBalance);
        long newNetWeight = accountCapsule.getFrozenV2BalanceWithDelegated(BANDWIDTH) / TRX_PRECISION;
        repo.addTotalNetWeight(newNetWeight - oldNetWeight);
        break;
      case ENERGY:
        long oldEnergyWeight = accountCapsule.getFrozenV2BalanceWithDelegated(ENERGY) / TRX_PRECISION;
        accountCapsule.addFrozenBalanceForEnergyV2(frozenBalance);
        long newEnergyWeight = accountCapsule.getFrozenV2BalanceWithDelegated(ENERGY) / TRX_PRECISION;
        repo.addTotalEnergyWeight(newEnergyWeight - oldEnergyWeight);
        break;
      case TRON_POWER:
        long oldTPWeight = accountCapsule.getTronPowerFrozenV2Balance() / TRX_PRECISION;
        accountCapsule.addFrozenForTronPowerV2(frozenBalance);
        long newTPWeight = accountCapsule.getTronPowerFrozenV2Balance() / TRX_PRECISION;
        repo.addTotalTronPowerWeight(newTPWeight - oldTPWeight);
        break;
      default:
        logger.debug("Resource Code Error.");
    }
```

**File:** actuator/src/main/java/org/tron/core/vm/nativecontract/UnfreezeBalanceV2Processor.java (L183-209)
```java
  public void updateTotalResourceWeight(AccountCapsule accountCapsule,
                                        Common.ResourceCode freezeType,
                                        long unfreezeBalance,
                                        Repository repo) {
    switch (freezeType) {
      case BANDWIDTH:
        long oldNetWeight = accountCapsule.getFrozenV2BalanceWithDelegated(BANDWIDTH) / TRX_PRECISION;
        accountCapsule.addFrozenBalanceForBandwidthV2(-unfreezeBalance);
        long newNetWeight = accountCapsule.getFrozenV2BalanceWithDelegated(BANDWIDTH) / TRX_PRECISION;
        repo.addTotalNetWeight(newNetWeight - oldNetWeight);
        break;
      case ENERGY:
        long oldEnergyWeight = accountCapsule.getFrozenV2BalanceWithDelegated(ENERGY) / TRX_PRECISION;
        accountCapsule.addFrozenBalanceForEnergyV2(-unfreezeBalance);
        long newEnergyWeight = accountCapsule.getFrozenV2BalanceWithDelegated(ENERGY) / TRX_PRECISION;
        repo.addTotalEnergyWeight(newEnergyWeight - oldEnergyWeight);
        break;
      case TRON_POWER:
        long oldTPWeight = accountCapsule.getTronPowerFrozenV2Balance() / TRX_PRECISION;
        accountCapsule.addFrozenForTronPowerV2(-unfreezeBalance);
        long newTPWeight = accountCapsule.getTronPowerFrozenV2Balance() / TRX_PRECISION;
        repo.addTotalTronPowerWeight(newTPWeight - oldTPWeight);
        break;
      default:
        //this should never happen
        break;
    }
```

**File:** actuator/src/main/java/org/tron/core/vm/nativecontract/CancelAllUnfreezeV2Processor.java (L77-102)
```java
  public void updateFrozenInfoAndTotalResourceWeight(
      AccountCapsule accountCapsule, Protocol.Account.UnFreezeV2 unFreezeV2, Repository repo) {
    switch (unFreezeV2.getType()) {
      case BANDWIDTH:
        long oldNetWeight = accountCapsule.getFrozenV2BalanceWithDelegated(BANDWIDTH) / TRX_PRECISION;
        accountCapsule.addFrozenBalanceForBandwidthV2(unFreezeV2.getUnfreezeAmount());
        long newNetWeight = accountCapsule.getFrozenV2BalanceWithDelegated(BANDWIDTH) / TRX_PRECISION;
        repo.addTotalNetWeight(newNetWeight - oldNetWeight);
        break;
      case ENERGY:
        long oldEnergyWeight = accountCapsule.getFrozenV2BalanceWithDelegated(ENERGY) / TRX_PRECISION;
        accountCapsule.addFrozenBalanceForEnergyV2(unFreezeV2.getUnfreezeAmount());
        long newEnergyWeight = accountCapsule.getFrozenV2BalanceWithDelegated(ENERGY) / TRX_PRECISION;
        repo.addTotalEnergyWeight(newEnergyWeight - oldEnergyWeight);
        break;
      case TRON_POWER:
        long oldTPWeight = accountCapsule.getTronPowerFrozenV2Balance() / TRX_PRECISION;
        accountCapsule.addFrozenForTronPowerV2(unFreezeV2.getUnfreezeAmount());
        long newTPWeight = accountCapsule.getTronPowerFrozenV2Balance() / TRX_PRECISION;
        repo.addTotalTronPowerWeight(newTPWeight - oldTPWeight);
        break;
      default:
        // this should never happen
        break;
    }
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

**File:** framework/src/test/java/org/tron/common/runtime/vm/FreezeV2Test.java (L933-1028)
```java
  private TVMTestResult unDelegateResourceWithException(
      byte[] callerAddr, byte[] contractAddr, byte[] receiverAddr, long amount, long res)
      throws Exception {
    return triggerUnDelegateResource(
        callerAddr, contractAddr, REVERT, null, receiverAddr, amount, res);
  }

  private TVMTestResult suicide(byte[] callerAddr, byte[] contractAddr, byte[] inheritorAddr)
      throws Exception {
    if (FastByteComparisons.isEqual(contractAddr, inheritorAddr)) {
      inheritorAddr = dbManager.getAccountStore().getBlackholeAddress();
    }
    DynamicPropertiesStore dynamicStore = dbManager.getDynamicPropertiesStore();
    long oldTotalNetWeight = dynamicStore.getTotalNetWeight();
    long oldTotalEnergyWeight = dynamicStore.getTotalEnergyWeight();
    long now = dynamicStore.getLatestBlockHeaderTimestamp();

    AccountStore accountStore = dbManager.getAccountStore();
    AccountCapsule oldContract = accountStore.get(contractAddr);
    AccountCapsule oldInheritor = accountStore.get(inheritorAddr);
    long oldBalanceOfInheritor = 0;
    long oldInheritorFrozenBalance = 0;
    long oldInheritorBandwidthUsage = 0;
    long oldInheritorEnergyUsage = 0;
    if (oldInheritor != null) {
      oldBalanceOfInheritor = oldInheritor.getBalance();
      oldInheritorFrozenBalance = oldInheritor.getFrozenBalance();
      oldInheritorBandwidthUsage = oldInheritor.getUsage(BANDWIDTH);
      oldInheritorEnergyUsage = oldInheritor.getUsage(ENERGY);
    }
    BandwidthProcessor bandwidthProcessor = new BandwidthProcessor(ChainBaseManager.getInstance());
    bandwidthProcessor.updateUsage(oldContract);
    oldContract.setLatestConsumeTime(now);
    EnergyProcessor energyProcessor =
        new EnergyProcessor(
            dbManager.getDynamicPropertiesStore(),
            ChainBaseManager.getInstance().getAccountStore());
    energyProcessor.updateUsage(oldContract);
    oldContract.setLatestConsumeTimeForEnergy(now);

    TVMTestResult result = triggerSuicide(callerAddr, contractAddr, SUCCESS, null, inheritorAddr);

    Assert.assertNull(accountStore.get(contractAddr));
    AccountCapsule newInheritor = accountStore.get(inheritorAddr);
    Assert.assertNotNull(newInheritor);
    long expectedIncreasingBalance =
        oldContract.getBalance()
            + oldContract.getUnfrozenV2List().stream()
            .filter(unFreezeV2 -> unFreezeV2.getUnfreezeExpireTime() <= now)
            .mapToLong(Protocol.Account.UnFreezeV2::getUnfreezeAmount)
            .sum();
    if (FastByteComparisons.isEqual(
        inheritorAddr, dbManager.getAccountStore().getBlackholeAddress())) {
      Assert.assertEquals(
          expectedIncreasingBalance,
          newInheritor.getBalance() - oldBalanceOfInheritor - result.getReceipt().getEnergyFee());
    } else {
      Assert.assertEquals(
          expectedIncreasingBalance, newInheritor.getBalance() - oldBalanceOfInheritor);
    }

    Assert.assertEquals(0, oldContract.getDelegatedFrozenV2BalanceForBandwidth());
    Assert.assertEquals(0, oldContract.getDelegatedFrozenV2BalanceForEnergy());
    Assert.assertEquals(
        oldContract.getFrozenBalance(),
        newInheritor.getFrozenBalance() - oldInheritorFrozenBalance);
    if (oldInheritor != null) {
      if (oldContract.getNetUsage() > 0) {
        bandwidthProcessor.unDelegateIncrease(oldInheritor, oldContract, oldContract.getNetUsage(),
            Common.ResourceCode.BANDWIDTH, now);
        Assert.assertEquals(
            oldInheritor.getNetUsage(), newInheritor.getNetUsage() - oldInheritorBandwidthUsage);
        Assert.assertEquals(
            ChainBaseManager.getInstance().getHeadSlot(), newInheritor.getLatestConsumeTime());
      }
      if (oldContract.getEnergyUsage() > 0) {
        energyProcessor.unDelegateIncrease(oldInheritor, oldContract,
            oldContract.getEnergyUsage(), Common.ResourceCode.ENERGY, now);
        Assert.assertEquals(
            oldInheritor.getEnergyUsage(), newInheritor.getEnergyUsage() - oldInheritorEnergyUsage);
        Assert.assertEquals(
            ChainBaseManager.getInstance().getHeadSlot(),
            newInheritor.getLatestConsumeTimeForEnergy());
      }
    }

    long newTotalNetWeight = dynamicStore.getTotalNetWeight();
    long newTotalEnergyWeight = dynamicStore.getTotalEnergyWeight();
    Assert.assertEquals(
        oldContract.getFrozenBalance(), (oldTotalNetWeight - newTotalNetWeight) * TRX_PRECISION);
    Assert.assertEquals(
        oldContract.getEnergyFrozenBalance(),
        (oldTotalEnergyWeight - newTotalEnergyWeight) * TRX_PRECISION);

    return result;
  }
```
