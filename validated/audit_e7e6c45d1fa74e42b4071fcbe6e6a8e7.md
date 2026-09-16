Found it: `UnDelegateResourceActuator.execute` (actuator/src/main/java/org/tron/core/actuator/UnDelegateResourceActuator.java) modifies the owner's frozen/delegated resource balances and the receiver's usage accounting, but — unlike `UnfreezeBalanceActuator`, `UnfreezeBalanceV2Actuator`, `VoteWitnessActuator`, and `WithdrawBalanceActuator` — it never calls `mortgageService.withdrawReward(ownerAddress)` before mutating state. This is analogous to the reported bug class (stale reward snapshot / accumulator not refreshed before a balance-affecting state change).

### Title
Missing reward settlement before resource undelegation causes incorrect vote-reward accrual - (File: actuator/src/main/java/org/tron/core/actuator/UnDelegateResourceActuator.java)

### Summary
`UnDelegateResourceActuator.execute()` mutates the owner account's frozen/delegated resource balances (which back TronPower and thus voting weight) without first settling pending vote rewards via `MortgageService.withdrawReward`, unlike sibling actuators that touch the same TronPower/vote state.

### Finding Description
TRON's vote-reward accounting (`MortgageService.withdrawReward` / `computeReward`, chainbase/src/main/java/org/tron/core/service/MortgageService.java:89-134) works on a cycle-snapshot model: it computes rewards for `[beginCycle, currentCycle)` using the account's votes snapshot stored via `delegationStore.setAccountVote`, then advances `beginCycle`/`endCycle`. Every actuator that can change the votes-affecting state (frozen balance / TronPower / votes list) is expected to call `withdrawReward(ownerAddress)` first, to correctly settle rewards attributable to the *old* voting-power state before it changes. This pattern is followed by: [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) 

However, `UnDelegateResourceActuator.execute()` directly modifies `ownerCapsule`'s delegated/frozen resource balances (`addDelegatedFrozenV2BalanceForBandwidth`, `addFrozenBalanceForBandwidthV2`, etc.), which reduce TronPower and hence subsequent voting/reward-eligible power, without calling `mortgageService.withdrawReward(ownerAddress)` anywhere in the method: [5](#0-4) 

Since the TVM native-contract counterpart `UnfreezeBalanceV2Processor` (which handles the analogous unfreeze/undelegate-like state transition inside contract calls) does call `VoteRewardUtil.withdrawReward(ownerAddress, repo)` before mutating state: [6](#0-5) 

the omission in `UnDelegateResourceActuator` is an inconsistency in the same "settle-before-mutate" invariant that the original report's `_beforeTokenTransfer`/`_updateUser` mechanism relies on.

### Impact Explanation
Because `withdrawReward` uses cycle-boundary snapshots of the account's votes list (`delegationStore.getAccountVote`) to compute reward deltas per cycle using `getWitnessVi`, failing to call it before the frozen/delegated-resource balance mutates means the account's `beginCycle`/`endCycle` bookkeeping and the eventually-snapshotted votes may no longer correspond to the TronPower actually held during the intervening cycles at the time rewards are later withdrawn. This can permanently misattribute reward accrual for the affected account across a cycle boundary — a correctness/fund-accounting defect in the reward system, not merely a resource accounting bug — reachable by any account owner issuing a normal `UnDelegateResourceContract` transaction.

### Likelihood Explanation
Any unprivileged account owner who has delegated resources and also participates in the vote-reward mechanism can trigger this by broadcasting an `UnDelegateResourceContract` transaction near a maintenance/cycle boundary — no special privilege required. The relevant `dynamicStore.allowChangeDelegation()`/`supportUnfreezeDelay()` feature flags must be enabled (already the case on current mainnet-equivalent configuration), so the path is broadly reachable.

### Recommendation
Add a call to `mortgageService.withdrawReward(ownerAddress)` (and, if a symmetric effect is intended for the receiver's own vote-affecting state, consider whether `receiverAddress` requires similar settlement) at the start of `UnDelegateResourceActuator.execute()`, mirroring the pattern already used in `UnfreezeBalanceActuator`, `UnfreezeBalanceV2Actuator`, `VoteWitnessActuator`, and `UnfreezeBalanceV2Processor`.

### Proof of Concept
1. Account A delegates resource to B, then votes for witnesses, accruing reward eligibility.
2. Near a maintenance cycle boundary, A submits `UnDelegateResourceContract` to reclaim delegated balance back to its own frozen balance (`UnDelegateResourceActuator.execute`, lines 133-168).
3. Because `mortgageService.withdrawReward(ownerAddress)` is never invoked here (contrast with `UnfreezeBalanceV2Actuator.execute` line 72), the reward-cycle snapshot logic in `MortgageService.withdrawReward` computes/advances cycles using stale vote/tron-power state, producing incorrect reward accrual when A later calls `withdrawReward`/`WithdrawBalanceActuator`. [7](#0-6)

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/UnfreezeBalanceActuator.java (L71-76)
```java
    byte[] ownerAddress = unfreezeBalanceContract.getOwnerAddress().toByteArray();

    //
    mortgageService.withdrawReward(ownerAddress);

    AccountCapsule accountCapsule = accountStore.get(ownerAddress);
```

**File:** actuator/src/main/java/org/tron/core/actuator/UnfreezeBalanceV2Actuator.java (L69-74)
```java
    byte[] ownerAddress = unfreezeBalanceV2Contract.getOwnerAddress().toByteArray();
    long now = dynamicStore.getLatestBlockHeaderTimestamp();

    mortgageService.withdrawReward(ownerAddress);

    AccountCapsule accountCapsule = accountStore.get(ownerAddress);
```

**File:** actuator/src/main/java/org/tron/core/actuator/VoteWitnessActuator.java (L160-162)
```java
    //
    mortgageService.withdrawReward(ownerAddress);

```

**File:** actuator/src/main/java/org/tron/core/actuator/WithdrawBalanceActuator.java (L54-55)
```java
    mortgageService.withdrawReward(withdrawBalanceContract.getOwnerAddress()
        .toByteArray());
```

**File:** actuator/src/main/java/org/tron/core/actuator/UnDelegateResourceActuator.java (L133-168)
```java
    // modify owner Account
    AccountCapsule ownerCapsule = accountStore.get(ownerAddress);
    switch (unDelegateResourceContract.getResource()) {
      case BANDWIDTH: {
        unlockResource.addFrozenBalanceForBandwidth(-unDelegateBalance, 0);

        ownerCapsule.addDelegatedFrozenV2BalanceForBandwidth(-unDelegateBalance);
        ownerCapsule.addFrozenBalanceForBandwidthV2(unDelegateBalance);

        BandwidthProcessor processor = new BandwidthProcessor(chainBaseManager);

        long now = chainBaseManager.getHeadSlot();
        if (Objects.nonNull(receiverCapsule) && transferUsage > 0) {
          processor.unDelegateIncrease(ownerCapsule, receiverCapsule,
              transferUsage, BANDWIDTH, now);
        }
      }
      break;
      case ENERGY: {
        unlockResource.addFrozenBalanceForEnergy(-unDelegateBalance, 0);

        ownerCapsule.addDelegatedFrozenV2BalanceForEnergy(-unDelegateBalance);
        ownerCapsule.addFrozenBalanceForEnergyV2(unDelegateBalance);

        EnergyProcessor processor = new EnergyProcessor(dynamicStore, accountStore);

        long now = chainBaseManager.getHeadSlot();
        if (Objects.nonNull(receiverCapsule) && transferUsage > 0) {
          processor.unDelegateIncrease(ownerCapsule, receiverCapsule, transferUsage, ENERGY, now);
        }
      }
      break;
      default:
        //this should never happen
        break;
    }
```

**File:** actuator/src/main/java/org/tron/core/vm/nativecontract/UnfreezeBalanceV2Processor.java (L123-127)
```java
  public long execute(UnfreezeBalanceV2Param param, Repository repo) {
    byte[] ownerAddress = param.getOwnerAddress();
    long unfreezeBalance = param.getUnfreezeBalance();
    VoteRewardUtil.withdrawReward(ownerAddress, repo);

```

**File:** chainbase/src/main/java/org/tron/core/service/MortgageService.java (L89-134)
```java
  public void withdrawReward(byte[] address) {
    if (!dynamicPropertiesStore.allowChangeDelegation()) {
      return;
    }
    AccountCapsule accountCapsule = accountStore.get(address);
    long beginCycle = delegationStore.getBeginCycle(address);
    long endCycle = delegationStore.getEndCycle(address);
    long currentCycle = dynamicPropertiesStore.getCurrentCycleNumber();
    long reward = 0;
    if (beginCycle > currentCycle || accountCapsule == null) {
      return;
    }
    if (beginCycle == currentCycle) {
      AccountCapsule account = delegationStore.getAccountVote(beginCycle, address);
      if (account != null) {
        return;
      }
    }
    //withdraw the latest cycle reward
    if (beginCycle + 1 == endCycle && beginCycle < currentCycle) {
      AccountCapsule account = delegationStore.getAccountVote(beginCycle, address);
      if (account != null) {
        reward = computeReward(beginCycle, endCycle, account);
        adjustAllowance(address, reward);
        reward = 0;
        logger.info("Latest cycle reward {}, {}.", beginCycle, account.getVotesList());
      }
      beginCycle += 1;
    }
    //
    endCycle = currentCycle;
    if (CollectionUtils.isEmpty(accountCapsule.getVotesList())) {
      delegationStore.setBeginCycle(address, endCycle + 1);
      return;
    }
    if (beginCycle < endCycle) {
      reward += computeReward(beginCycle, endCycle, accountCapsule);
      adjustAllowance(address, reward);
    }
    delegationStore.setBeginCycle(address, endCycle);
    delegationStore.setEndCycle(address, endCycle + 1);
    delegationStore.setAccountVote(endCycle, address, accountCapsule);
    logger.info("Adjust {} allowance {}, now currentCycle {}, beginCycle {}, endCycle {}, "
            + "account vote {}.", Hex.toHexString(address), reward, currentCycle,
        beginCycle, endCycle, accountCapsule.getVotesList());
  }
```
