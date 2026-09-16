### Title
`UpdateBrokerageActuator` changes brokerage without settling pending witness reward, enabling reward miscalculation - (File: `actuator/src/main/java/org/tron/core/actuator/UpdateBrokerageActuator.java`)

### Summary
`UpdateBrokerageActuator.execute()` updates a witness's brokerage ratio via `delegationStore.setBrokerage(ownerAddress, brokerage)` without first calling `mortgageService.withdrawReward(ownerAddress)` to settle/checkpoint the witness's own pending vote reward, unlike sibling actuators (`VoteWitnessActuator`, `UnfreezeBalanceV2Actuator`) that always call `mortgageService.withdrawReward(...)` before mutating delegation-related state.

### Finding Description
`UpdateBrokerageActuator.execute()` directly writes the new brokerage value: [1](#0-0) 

Compare with `VoteWitnessActuator.countVoteAccount()` and `UnfreezeBalanceV2Actuator.execute()`, both of which call `mortgageService.withdrawReward(ownerAddress)` before modifying vote/freeze state, in order to correctly checkpoint reward accounting cycles (`beginCycle`/`endCycle`/`accountVote` snapshot) before the parameters that affect future reward computation change: [2](#0-1) [3](#0-2) 

`MortgageService.withdrawReward()` relies on `delegationStore.getAccountVote(beginCycle, address)` snapshots taken at settlement time, and reward computation (`computeReward`) uses per-cycle brokerage via `delegationStore.getBrokerage(cycle, witnessAddress)`: [4](#0-3) 

At every maintenance cycle, `MaintenanceManager.doMaintenance()` copies the witness's current brokerage (`delegationStore.getBrokerage(witness.createDbKey())`, i.e. the "current" `-1` slot set by `UpdateBrokerageActuator`) forward into the specific `nextCycle` brokerage record: [5](#0-4) 

Because `UpdateBrokerageActuator` never triggers `withdrawReward` for the owner address before changing the brokerage setting, if a witness's own delegation reward-settlement state (`beginCycle`) is behind the current cycle when brokerage is changed, the eventual `computeReward` call (invoked lazily on a later `withdrawReward`) can span a reward-cycle range whose brokerage rate was retroactively altered by an intervening `updateBrokerage` call, analogous to the reported `updateYieldStrategy` bug where switching a state-affecting parameter without withdrawing/settling first corrupts a value used to compute payouts.

### Impact Explanation
Impact is limited compared to the original Sherlock report because `MortgageService.payReward()` (block/standby-witness reward payout) already reads `delegationStore.getBrokerage(cycle, witnessAddress)` per-cycle at the moment of payment, and `MaintenanceManager.doMaintenance()` snapshots the brokerage into a fixed per-cycle slot at the start of every new cycle. This means the specific per-cycle brokerage used for `addReward`/`adjustAllowance` at payout time is fixed once the cycle rolls over, which narrows — but does not fully eliminate — the reachable window in which an un-settled `withdrawReward` combined with a same-cycle `updateBrokerage` call could shift the split between witness self-reward (allowance) and voter reward. I was not able to fully trace whether such a race is actually exploitable to produce unbacked funds or an economically meaningful reward-shift without additional runtime testing (e.g. simulating multiple `updateBrokerage` calls interleaved with `withdrawReward`/maintenance cycles), so I flag this as **uncertain/unconfirmed** rather than a proven high-impact bug.

### Likelihood Explanation
Likelihood is low: `UpdateBrokerageContract` requires the caller to be the account itself (owner-signed) and only affects that witness's own brokerage split, and the per-cycle snapshot mechanism in `MaintenanceManager` bounds the blast radius to at most the current, unsettled cycle. This is a narrower and weaker analog than the original report's TVL-scale fund-freezing bug.

### Recommendation
Given the uncertainty about actual exploitability, this should be validated with targeted testing (multiple `updateBrokerage` calls within the same cycle, followed by delayed `withdrawReward`, checking resulting allowance/reward split against expected values) before treating it as a confirmed vulnerability. If confirmed, the fix would be to call `mortgageService.withdrawReward(ownerAddress)` in `UpdateBrokerageActuator.execute()` before `delegationStore.setBrokerage(...)`, mirroring the pattern used in `VoteWitnessActuator` and `UnfreezeBalanceV2Actuator`.

### Proof of Concept
Not independently reproduced. Conceptually:
1. Witness `W` votes for itself and accrues reward across cycles without calling `withdrawReward` (so `beginCycle` stays behind `currentCycle`).
2. `W` calls `updateBrokerage` multiple times within/across unsettled cycles via `UpdateBrokerageServlet` → `UpdateBrokerageActuator.execute()`, which never checkpoints reward state.
3. `W` (or anyone triggering vote/unfreeze on `W`, which does call `withdrawReward`) eventually settles, and `computeReward` runs over the unsettled cycle range using whichever brokerage was recorded per-cycle by `MaintenanceManager`.

This proof-of-concept could not be fully validated against the per-cycle snapshot logic in `MaintenanceManager.doMaintenance()`, so the actual fund-impact remains unconfirmed with the tools available in this session.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/UpdateBrokerageActuator.java (L49-55)
```java
    byte[] ownerAddress = updateBrokerageContract.getOwnerAddress().toByteArray();
    int brokerage = updateBrokerageContract.getBrokerage();

    delegationStore.setBrokerage(ownerAddress, brokerage);
    ret.setStatus(fee, code.SUCESS);

    return true;
```

**File:** actuator/src/main/java/org/tron/core/actuator/VoteWitnessActuator.java (L152-163)
```java
  private void countVoteAccount(VoteWitnessContract voteContract) {
    AccountStore accountStore = chainBaseManager.getAccountStore();
    VotesStore votesStore = chainBaseManager.getVotesStore();
    MortgageService mortgageService = chainBaseManager.getMortgageService();
    byte[] ownerAddress = voteContract.getOwnerAddress().toByteArray();

    VotesCapsule votesCapsule;

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

**File:** chainbase/src/main/java/org/tron/core/service/MortgageService.java (L79-134)
```java
  private void payReward(byte[] witnessAddress, long value) {
    long cycle = dynamicPropertiesStore.getCurrentCycleNumber();
    int brokerage = delegationStore.getBrokerage(cycle, witnessAddress);
    double brokerageRate = (double) brokerage / 100;
    long brokerageAmount = (long) (brokerageRate * value);
    value -= brokerageAmount;
    delegationStore.addReward(cycle, witnessAddress, value);
    adjustAllowance(witnessAddress, brokerageAmount);
  }

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
