Based on the report's core bug class — an **instantaneous state snapshot** (BAYC NFT ownership at a single point in time) being used to authorize a value-bearing action (airdrop claim) that can be borrowed and returned atomically — the closest reachable analog in java-tron is the DPoS voting‑reward system, where a **momentary vote snapshot taken via `MortgageService.withdrawReward`** is applied to an entire reward cycle regardless of how long the underlying stake was actually held.

### Title
Voting-reward snapshot lets an attacker earn a full cycle's SR reward for a stake held only momentarily (freeze→vote→unfreeze in one transaction) - ([File: chainbase/src/main/java/org/tron/core/service/MortgageService.java])

### Summary
`MortgageService.withdrawReward` records a snapshot of an account's current `votesList` (`delegationStore.setAccountVote(endCycle, address, accountCapsule)`) whenever it's invoked — which happens automatically inside `VoteWitnessActuator`, `UnfreezeBalanceActuator`/`UnfreezeBalanceV2Actuator`, and the TVM native `WithdrawRewardProcessor`. This snapshot is later used by `computeReward(beginCycle, endCycle, accountCapsule)` to pay out the **entire** cycle's reward as if the voter held that exact vote weight for the whole cycle duration, with no time-weighting of when in the cycle the vote/freeze actually took place. [1](#0-0) 

### Finding Description
`computeReward` distributes a cycle's total SR reward proportionally to `vote.getValue()` (the frozen TronPower amount converted into votes) recorded in the snapshot, using `delegationStore.getWitnessVi` deltas across the whole `[beginCycle, endCycle)` range: [2](#0-1) 

The snapshot itself is only taken at the moment `withdrawReward` runs, and this is triggered directly by user-controlled actuators:
- `VoteWitnessActuator.countVoteAccount` calls `mortgageService.withdrawReward(ownerAddress)` before recording new votes. [3](#0-2) 
- `UnfreezeBalanceV2Actuator`/`UnfreezeBalanceV2Processor` also call `VoteRewardUtil.withdrawReward`/`mortgageService.withdrawReward` before adjusting the account's frozen balance and votes. [4](#0-3) 
- The TVM exposes the same primitive directly to any smart contract via `Program.withdrawReward()`, which invokes `WithdrawRewardProcessor`. [5](#0-4) 

Because `FreezeBalanceV2Contract`/`UnfreezeBalanceV2Contract` have no minimum holding period before TronPower can be un-frozen (only the eventual TRX withdrawal is time-locked), and all of freeze → vote → unfreeze can be chained together atomically by a single smart contract within one transaction (exactly as demonstrated in the test harness, which issues `freezeBalance` then `voteWitness` calls back-to-back via `triggerContract`), an attacker can:
1. Freeze a large TRX balance to obtain TronPower.
2. Vote for a witness, which invokes `withdrawReward` and snapshots the inflated `votesList` for the current cycle into `delegationStore`.
3. Immediately `unfreezeBalanceV2` in the same transaction, returning the stake and TronPower to zero. [6](#0-5) 

When that cycle later completes, `computeReward` still pays out the attacker's share of the whole cycle's reward pool using the stale, momentary snapshot — exactly mirroring the APE Coin bug class where an instantaneous, flash-borrowed state (BAYC holding / TronPower) is treated as durable proof-of-eligibility for a period-based payout (airdrop / cycle reward).

### Impact Explanation
An attacker with access to a large amount of TRX for a single block/transaction can capture a share of the SR voting reward pool for an entire cycle without providing the sustained economic backing (locked TronPower) that the reward-distribution design assumes other voters provide. This dilutes/steals reward-pool funds away from legitimate long-term voters — an unauthorized, unbacked balance/reward allocation, satisfying the "theft of funds" / "unbacked balance" criteria.

### Likelihood Explanation
Reachable by any unprivileged account via ordinary signed `FreezeBalanceV2Contract`, `VoteWitnessContract`, `UnfreezeBalanceV2Contract` transactions, or entirely from within one TVM contract call using `Program.withdrawReward()` plus the native freeze/vote/unfreeze precompiles. No special privileges, no waiting period, no cross-block synchronization is required to establish the snapshot — only momentary capital.

### Recommendation
Weight cycle reward computation by the actual duration/fraction of the cycle that TronPower/votes were held (or snapshot votes only at fixed cycle boundaries such as maintenance, not on every user-triggered `withdrawReward` call), and/or enforce a minimum holding period between `FreezeBalanceV2`/vote and `UnfreezeBalanceV2` before the vote snapshot used for `computeReward` is considered valid for a cycle.

### Proof of Concept
1. Attacker account freezes a large TRX amount via `FreezeBalanceV2Contract` (TRON_POWER type) to obtain TronPower — `FreezeBalanceProcessor.execute`.
2. In the same transaction (or via a contract chaining calls), submit `VoteWitnessContract` for a witness; this internally calls `MortgageService.withdrawReward`, which records `delegationStore.setAccountVote(currentCycle, attacker, accountWithInflatedVotes)`.
3. Immediately submit `UnfreezeBalanceV2Contract` to reclaim the frozen TRX, zeroing the attacker's TronPower/votes going forward — `UnfreezeBalanceV2Processor.execute`.
4. After the current cycle completes (next maintenance), attacker calls `WithdrawRewardContract`/`queryReward`; `MortgageService.computeReward` pays out the attacker's proportional share of that full cycle's SR reward pool based on the stale snapshot from step 2, even though TronPower was held for only a fraction of the cycle. [7](#0-6)

### Citations

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

**File:** chainbase/src/main/java/org/tron/core/service/MortgageService.java (L199-230)
```java
  private long computeReward(long beginCycle, long endCycle, AccountCapsule accountCapsule) {
    if (beginCycle >= endCycle) {
      return 0;
    }

    long reward = 0;
    long newAlgorithmCycle = dynamicPropertiesStore.getNewRewardAlgorithmEffectiveCycle();
    List<Pair<byte[], Long>> srAddresses = accountCapsule.getVotesList().stream()
        .map(vote -> new Pair<>(vote.getVoteAddress().toByteArray(), vote.getVoteCount()))
        .collect(Collectors.toList());
    if (beginCycle < newAlgorithmCycle) {
      long oldEndCycle = min(endCycle, newAlgorithmCycle,
          dynamicPropertiesStore.disableJavaLangMath());
      reward = getOldReward(beginCycle, oldEndCycle, srAddresses);
      beginCycle = oldEndCycle;
    }
    if (beginCycle < endCycle) {
      for (Pair<byte[], Long>  vote : srAddresses) {
        byte[] srAddress = vote.getKey();
        BigInteger beginVi = delegationStore.getWitnessVi(beginCycle - 1, srAddress);
        BigInteger endVi = delegationStore.getWitnessVi(endCycle - 1, srAddress);
        BigInteger deltaVi = endVi.subtract(beginVi);
        if (deltaVi.signum() <= 0) {
          continue;
        }
        long userVote = vote.getValue();
        reward += deltaVi.multiply(BigInteger.valueOf(userVote))
            .divide(DelegationStore.DECIMAL_OF_VI_REWARD).longValue();
      }
    }
    return reward;
  }
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

**File:** actuator/src/main/java/org/tron/core/vm/nativecontract/UnfreezeBalanceV2Processor.java (L123-130)
```java
  public long execute(UnfreezeBalanceV2Param param, Repository repo) {
    byte[] ownerAddress = param.getOwnerAddress();
    long unfreezeBalance = param.getUnfreezeBalance();
    VoteRewardUtil.withdrawReward(ownerAddress, repo);

    AccountCapsule accountCapsule = repo.getAccount(ownerAddress);
    long now = repo.getDynamicPropertiesStore().getLatestBlockHeaderTimestamp();

```

**File:** actuator/src/main/java/org/tron/core/vm/program/Program.java (L2340-2359)
```java
  public long withdrawReward() {
    Repository repository = getContractState().newRepositoryChild();
    byte[] owner = getContextAddress();

    increaseNonce();
    InternalTransaction internalTx = addInternalTx(null, owner, owner, 0, null,
        "withdrawReward", nonce, null);

    WithdrawRewardParam param = new WithdrawRewardParam();
    param.setOwnerAddress(owner);
    param.setNowInMs(getTimestamp().longValue() * 1000);
    try {
      WithdrawRewardProcessor processor = new WithdrawRewardProcessor();
      processor.validate(param, repository);
      long allowance = processor.execute(param, repository);
      repository.commit();
      if (internalTx != null) {
        internalTx.setValue(allowance);
      }
      return allowance;
```

**File:** framework/src/test/java/org/tron/common/runtime/vm/VoteTest.java (L826-836)
```java
  private void freezeBalance(byte[] contract) throws Exception {
    triggerContract(contract, SUCCESS, null,
        freezeMethod, StringUtil.encode58Check(contract), freezeUnit, 1);
  }

  private void voteWitness(byte[] contract,
                           List<String> witnessList,
                           List<Long> tronPowerList) throws Exception {
    triggerContract(contract, SUCCESS, getEqualConsumer(1),
        voteMethod, witnessList, tronPowerList);
  }
```
