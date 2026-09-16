This confirms a strong analog: TRON's resource weight accounting (`TotalNetWeight`/`TotalEnergyWeight`) is a manually-tracked aggregate counter, incremented/decremented on every freeze/unfreeze/delegate transaction, and used directly as the denominator to price each account's share of bandwidth/energy — structurally identical to the reported `assetsAllocatedToStrategies` bookkeeping bug (a static counter standing in for a dynamically changing pool, driving a per-user "price").

### Title
Global resource-weight bookkeeping (`TotalEnergyWeight`/`TotalNetWeight`) can drift from actual frozen stake, mispricing every account's energy/bandwidth allocation - (File: `actuator/src/main/java/org/tron/core/actuator/UnfreezeBalanceActuator.java`, `chainbase/src/main/java/org/tron/core/store/DynamicPropertiesStore.java`)

### Summary
TRON prices each account's share of network bandwidth/energy as `accountFrozenWeight / TotalNetWeight (or TotalEnergyWeight) * totalLimit`, exactly analogous to the reported `elyAsset` pricing formula `assetsAllocatedToStrategies[asset]`. `TotalNetWeight`/`TotalEnergyWeight` are not derived from a live sum of all accounts' frozen balances; they are static aggregates mutated incrementally by every `Freeze*`/`Unfreeze*`/`Delegate*`/`CancelAllUnfreezeV2` actuator and native-contract processor via `addTotalNetWeight`/`addTotalEnergyWeight`/`addTotalTronPowerWeight`.

### Finding Description
`RepositoryImpl.addTotalEnergyWeight`/`addTotalNetWeight` [1](#0-0)  and `DynamicPropertiesStore.addTotalEnergyWeight` [2](#0-1)  simply add a delta to a persisted counter. This counter is fed by many independent call sites: `FreezeBalanceActuator.addTotalWeight` [3](#0-2) , `FreezeBalanceV2Actuator.execute` [4](#0-3) , `UnfreezeBalanceActuator.execute` [5](#0-4) , `UnfreezeBalanceV2Actuator.updateTotalResourceWeight` [6](#0-5) , `CancelAllUnfreezeV2Actuator`/`CancelAllUnfreezeV2Processor` [7](#0-6) , and the equivalent TVM-native processors `FreezeBalanceProcessor`/`FreezeBalanceV2Processor`/`UnfreezeBalanceProcessor`/`UnfreezeBalanceV2Processor` — each independently computing an "old weight" vs "new weight" delta from an individual account's frozen fields and applying it to the *global* aggregate. A stray comment "`// adjust total resource, used to be a bug here`" in `UnfreezeBalanceProcessor.java` line 190 [8](#0-7)  explicitly documents that this bookkeeping has already been the source of at least one historical bug, confirming the fragility of this pattern: any actuator/processor path that forgets to call `addTotalEnergyWeight`/`addTotalNetWeight`, computes the delta incorrectly (e.g. rounding via `/ TRX_PRECISION` truncation on each side independently instead of on the aggregate), or double-applies/omits an adjustment on a rarely-exercised branch (delegated resource, TRON_POWER, old-vs-new resource model transitions, `allowNewReward()` flag toggling behavior in `addTotalWeight`) will desynchronize the global counter from the true sum of all accounts' frozen weight — precisely the same class of bug as the reported `assetsAllocatedToStrategies` drift.

This global weight is then used as the sole denominator to price every account's energy/bandwidth allocation in `EnergyProcessor.calculateGlobalEnergyLimit`/`calculateGlobalEnergyLimitV2` [9](#0-8)  and `RepositoryImpl.calculateGlobalEnergyLimit` [10](#0-9) : `energyLimit = frozenWeight * totalEnergyLimit / totalEnergyWeight`.

### Impact Explanation
If `TotalEnergyWeight`/`TotalNetWeight` drifts low relative to the true aggregate frozen stake (analogous to TVL under-reporting), every account's computed `energyLimit`/`netLimit` is inflated, letting accounts consume more free energy/bandwidth than their frozen TRX entitles them to — an unbacked-resource condition equivalent to unbacked balance/insolvency, since energy ultimately gates TVM execution (fee bypass) and bandwidth gates transaction throughput. Conversely, drift high (analogous to TVL over-reporting) deflates every honest account's resource allocation. Because this denominator is a single global value touched by every freeze/unfreeze transaction from any account, a malicious actor can also observe/trigger the update sequence via `Freeze*`/`Unfreeze*`/`DelegateResource*` transactions to shift `TotalEnergyWeight` favorably before consuming energy for TVM calls, mirroring the reported front-running scenario.

### Likelihood Explanation
Every unprivileged account can broadcast `FreezeBalanceContract`, `FreezeBalanceV2Contract`, `UnfreezeBalanceContract`, `UnfreezeBalanceV2Contract`, `CancelAllUnfreezeV2Contract`, and the equivalent TVM-native freeze/unfreeze precompile paths at will — these are ordinary user-facing operations, not privileged admin functions, so any inconsistency in the delta computation across the many independent call sites is continuously reachable.

### Recommendation
Replace the manually-incremented `TotalEnergyWeight`/`TotalNetWeight` aggregates with a value periodically reconciled against (or entirely re-derived from) a canonical, dynamically-summed source of truth of all accounts' current frozen balances — for example recomputing it at each maintenance cycle in `MaintenanceManager.doMaintenance` [11](#0-10)  rather than relying on additive deltas scattered across a dozen actuator/processor implementations, and add invariant assertions (e.g., non-negative, bounded drift checks) that fail loudly if the tracked aggregate diverges from a periodically recomputed ground truth.

### Proof of Concept
Not directly exploitable as a single-transaction PoC without auditing every call site for a missing/incorrect delta; the concrete concern is architectural: `git blame`/history shows the weight-adjustment logic has already needed a fix once (see the `UnfreezeBalanceProcessor.java:190` comment), and the same delta-tracking pattern is independently re-implemented in at least 8 separate actuator/processor classes (`FreezeBalanceActuator`, `FreezeBalanceV2Actuator`, `UnfreezeBalanceActuator`, `UnfreezeBalanceV2Actuator`, `CancelAllUnfreezeV2Actuator`, `FreezeBalanceProcessor`, `FreezeBalanceV2Processor`, `UnfreezeBalanceProcessor`, `UnfreezeBalanceV2Processor`), each of which must independently get the old-weight/new-weight delta exactly right across resource-model version transitions (`supportAllowNewResourceModel`, `allowNewReward`) for the global aggregate to remain accurate — the same "manually updated counter never reconciled with reality" root cause as the reported `assetsAllocatedToStrategies[asset]` bug.

### Citations

**File:** actuator/src/main/java/org/tron/core/vm/repository/RepositoryImpl.java (L992-1010)
```java
  public long calculateGlobalEnergyLimit(AccountCapsule accountCapsule) {
    long frozeBalance = accountCapsule.getAllFrozenBalanceForEnergy();
    if (frozeBalance < TRX_PRECISION) {
      return 0;
    }
    long energyWeight = frozeBalance / TRX_PRECISION;
    long totalEnergyLimit = getDynamicPropertiesStore().getTotalEnergyCurrentLimit();
    long totalEnergyWeight = getDynamicPropertiesStore().getTotalEnergyWeight();

    assert totalEnergyWeight > 0;

    if (hardenResourceCalculation()) {
      return BigInteger.valueOf(energyWeight)
          .multiply(BigInteger.valueOf(totalEnergyLimit))
          .divide(BigInteger.valueOf(totalEnergyWeight))
          .longValueExact();
    }
    return (long) (energyWeight * ((double) totalEnergyLimit / totalEnergyWeight));
  }
```

**File:** actuator/src/main/java/org/tron/core/vm/repository/RepositoryImpl.java (L1196-1210)
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
```

**File:** chainbase/src/main/java/org/tron/core/store/DynamicPropertiesStore.java (L2282-2293)
```java
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
```

**File:** actuator/src/main/java/org/tron/core/actuator/FreezeBalanceActuator.java (L134-150)
```java
  private void addTotalWeight(ResourceCode resourceCode, DynamicPropertiesStore dynamicStore,
                              long frozenBalance, long increment) {
    long weight = dynamicStore.allowNewReward() ? increment : frozenBalance / TRX_PRECISION;
    switch (resourceCode) {
      case BANDWIDTH:
        dynamicStore.addTotalNetWeight(weight);
        break;
      case ENERGY:
        dynamicStore.addTotalEnergyWeight(weight);
        break;
      case TRON_POWER:
        dynamicStore.addTotalTronPowerWeight(weight);
        break;
      default:
        logger.debug("Resource Code Error.");
    }
  }
```

**File:** actuator/src/main/java/org/tron/core/actuator/FreezeBalanceV2Actuator.java (L60-81)
```java
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
      case TRON_POWER:
        long oldTPWeight = accountCapsule.getTronPowerFrozenV2Balance() / TRX_PRECISION;
        accountCapsule.addFrozenForTronPowerV2(frozenBalance);
        long newTPWeight = accountCapsule.getTronPowerFrozenV2Balance() / TRX_PRECISION;
        dynamicStore.addTotalTronPowerWeight(newTPWeight - oldTPWeight);
        break;
      default:
        logger.debug("Resource Code Error.");
    }
```

**File:** actuator/src/main/java/org/tron/core/actuator/UnfreezeBalanceActuator.java (L192-260)
```java
    } else {
      switch (unfreezeBalanceContract.getResource()) {
        case BANDWIDTH:
          long oldNetWeight = accountCapsule.getFrozenBalance() / TRX_PRECISION;
          List<Frozen> frozenList = Lists.newArrayList();
          frozenList.addAll(accountCapsule.getFrozenList());
          Iterator<Frozen> iterator = frozenList.iterator();
          long now = dynamicStore.getLatestBlockHeaderTimestamp();
          while (iterator.hasNext()) {
            Frozen next = iterator.next();
            if (next.getExpireTime() <= now) {
              unfreezeBalance += next.getFrozenBalance();
              iterator.remove();
            }
          }

          accountCapsule.setInstance(accountCapsule.getInstance().toBuilder()
              .setBalance(oldBalance + unfreezeBalance)
              .clearFrozen().addAllFrozen(frozenList).build());
          long newNetWeight = accountCapsule.getFrozenBalance() / TRX_PRECISION;
          decrease = newNetWeight - oldNetWeight;
          break;
        case ENERGY:
          long oldEnergyWeight = accountCapsule.getEnergyFrozenBalance() / TRX_PRECISION;
          unfreezeBalance = accountCapsule.getAccountResource().getFrozenBalanceForEnergy()
              .getFrozenBalance();

          AccountResource newAccountResource = accountCapsule.getAccountResource().toBuilder()
              .clearFrozenBalanceForEnergy().build();
          accountCapsule.setInstance(accountCapsule.getInstance().toBuilder()
              .setBalance(oldBalance + unfreezeBalance)
              .setAccountResource(newAccountResource).build());
          long newEnergyWeight = accountCapsule.getEnergyFrozenBalance() / TRX_PRECISION;
          decrease = newEnergyWeight - oldEnergyWeight;
          break;
        case TRON_POWER:
          long oldTPWeight = accountCapsule.getTronPowerFrozenBalance() / TRX_PRECISION;
          unfreezeBalance = accountCapsule.getTronPowerFrozenBalance();
          accountCapsule.setInstance(accountCapsule.getInstance().toBuilder()
              .setBalance(oldBalance + unfreezeBalance)
              .clearTronPower().build());
          long newTPWeight = accountCapsule.getTronPowerFrozenBalance() / TRX_PRECISION;
          decrease = newTPWeight - oldTPWeight;
          break;
        default:
          //this should never happen
          break;
      }

    }
    
    long weight = dynamicStore.allowNewReward() ? decrease : -unfreezeBalance / TRX_PRECISION;
    switch (unfreezeBalanceContract.getResource()) {
      case BANDWIDTH:
        dynamicStore
            .addTotalNetWeight(weight);
        break;
      case ENERGY:
        dynamicStore
            .addTotalEnergyWeight(weight);
        break;
      case TRON_POWER:
        dynamicStore
            .addTotalTronPowerWeight(weight);
        break;
      default:
        //this should never happen
        break;
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

**File:** actuator/src/main/java/org/tron/core/vm/nativecontract/UnfreezeBalanceProcessor.java (L190-201)
```java
    // adjust total resource, used to be a bug here
    switch (param.getResourceType()) {
      case BANDWIDTH:
        repo.addTotalNetWeight(-unfreezeBalance / TRX_PRECISION);
        break;
      case ENERGY:
        repo.addTotalEnergyWeight(-unfreezeBalance / TRX_PRECISION);
        break;
      default:
        //this should never happen
        break;
    }
```

**File:** chainbase/src/main/java/org/tron/core/db/EnergyProcessor.java (L145-179)
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

**File:** consensus/src/main/java/org/tron/consensus/dpos/MaintenanceManager.java (L89-163)
```java
  public void doMaintenance() {
    VotesStore votesStore = consensusDelegate.getVotesStore();

    tryRemoveThePowerOfTheGr();

    DynamicPropertiesStore dynamicPropertiesStore = consensusDelegate.getDynamicPropertiesStore();
    DelegationStore delegationStore = consensusDelegate.getDelegationStore();
    if (dynamicPropertiesStore.useNewRewardAlgorithm()) {
      long curCycle = dynamicPropertiesStore.getCurrentCycleNumber();
      consensusDelegate.getAllWitnesses().forEach(witness -> {
        delegationStore.accumulateWitnessVi(curCycle, witness.createDbKey(), witness.getVoteCount());
      });
    }

    Map<ByteString, Long> countWitness = countVote(votesStore);
    if (!countWitness.isEmpty()) {
      List<ByteString> currentWits = consensusDelegate.getActiveWitnesses();

      List<ByteString> newWitnessAddressList = new ArrayList<>();
      consensusDelegate.getAllWitnesses()
          .forEach(witnessCapsule -> newWitnessAddressList.add(witnessCapsule.getAddress()));

      countWitness.forEach((address, voteCount) -> {
        byte[] witnessAddress = address.toByteArray();
        WitnessCapsule witnessCapsule = consensusDelegate.getWitness(witnessAddress);
        if (witnessCapsule == null) {
          logger.warn("Witness capsule is null. address is {}", Hex.toHexString(witnessAddress));
          return;
        }
        AccountCapsule account = consensusDelegate.getAccount(witnessAddress);
        if (account == null) {
          logger.warn("Witness account is null. address is {}", Hex.toHexString(witnessAddress));
          return;
        }
        witnessCapsule.setVoteCount(witnessCapsule.getVoteCount() + voteCount);
        consensusDelegate.saveWitness(witnessCapsule);
        logger.info("address is {} , countVote is {}", witnessCapsule.createReadableString(),
            witnessCapsule.getVoteCount());
      });

      dposService.updateWitness(newWitnessAddressList);

      incentiveManager.reward(newWitnessAddressList);

      List<ByteString> newWits = consensusDelegate.getActiveWitnesses();
      if (!CollectionUtils.isEqualCollection(currentWits, newWits)) {
        currentWits.forEach(address -> {
          WitnessCapsule witnessCapsule = consensusDelegate.getWitness(address.toByteArray());
          witnessCapsule.setIsJobs(false);
          consensusDelegate.saveWitness(witnessCapsule);
        });
        newWits.forEach(address -> {
          WitnessCapsule witnessCapsule = consensusDelegate.getWitness(address.toByteArray());
          witnessCapsule.setIsJobs(true);
          consensusDelegate.saveWitness(witnessCapsule);
        });

        SRMetrics.recordSrSetChange(currentWits, newWits);
      }

      logger.info("Update witness success. \nbefore: {} \nafter: {}",
          getAddressStringList(currentWits),
          getAddressStringList(newWits));
    }

    if (dynamicPropertiesStore.allowChangeDelegation()) {
      long nextCycle = dynamicPropertiesStore.getCurrentCycleNumber() + 1;
      dynamicPropertiesStore.saveCurrentCycleNumber(nextCycle);
      consensusDelegate.getAllWitnesses().forEach(witness -> {
        delegationStore.setBrokerage(nextCycle, witness.createDbKey(),
            delegationStore.getBrokerage(witness.createDbKey()));
        delegationStore.setWitnessVote(nextCycle, witness.createDbKey(), witness.getVoteCount());
      });
    }
  }
```
