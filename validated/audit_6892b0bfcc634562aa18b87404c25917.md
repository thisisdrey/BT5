### Title
Inconsistent Fee/Reward Accumulator (Vi) Algorithm Selection Between TVM Vote Reward Path and Standard Withdraw Path Can Cause Incorrect or Zero Reward Calculation - (File: `actuator/src/main/java/org/tron/core/vm/utils/VoteRewardUtil.java`)

### Summary
The reported Kyber issue concerns a pool state counter (`tick`) whose accuracy directly feeds fee-growth and liquidity calculations; if it is not kept in sync, LPs get incorrect fee/reward amounts. The java-tron analog is the per-witness cumulative reward accumulator "Vi" (`WitnessVi`), which plays exactly the same role as Uniswap-style `feeGrowthGlobal` counters: it is diffed between two cycles to compute a voter's reward. There are **two independent implementations** that read/derive reward from this accumulator — `MortgageService` (used by the `WithdrawBalanceContract`/`VoteWitnessContract` actuators) and `VoteRewardUtil` (used by TVM native contracts triggered from smart-contract calls, i.e. `vote`, `withdrawreward`, `freezebalancecontract`/`unfreezebalancecontract` opcodes, and contract `SUICIDE`). Only the `MortgageService` implementation contains the logic that switches between the old per-cycle "vote-rate" reward algorithm and the new Vi-diff algorithm depending on `getNewRewardAlgorithmEffectiveCycle()`. The `VoteRewardUtil` implementation unconditionally uses the Vi-diff algorithm with no fallback, meaning it silently depends on the Vi accumulator being fully and correctly populated for all cycles being diffed — an assumption analogous to “pool tick counter is always in sync” from the report.

### Finding Description
`MortgageService.computeReward(beginCycle, endCycle, accountCapsule)` explicitly splits the calculation range at `newAlgorithmCycle = dynamicPropertiesStore.getNewRewardAlgorithmEffectiveCycle()`: [1](#0-0) 

For any span of cycles before `newAlgorithmCycle`, it falls back to `getOldReward()` (per-cycle vote-rate computation using `delegationStore.getReward`/`getWitnessVote`) instead of trusting the Vi accumulator: [2](#0-1) 

In contrast, `VoteRewardUtil.computeReward` (used by the TVM-triggered reward/vote path) performs a raw Vi diff for the *entire* `[beginCycle, endCycle)` range with no branching on `newAlgorithmCycle` at all: [3](#0-2) 

This code path is reachable from an unprivileged, signed transaction: any account can call `TriggerSmartContract` executing a contract that performs `vote`, `withdrawreward`, or freeze/unfreeze operations, which route to `VoteWitnessProcessor.execute`, `WithdrawRewardProcessor.execute`, `UnfreezeBalanceProcessor.execute`, or `Program.withdrawReward()`/`withdrawRewardAndCancelVote()`, all of which call `VoteRewardUtil.withdrawReward`/`queryReward`: [4](#0-3) [5](#0-4) [6](#0-5) 

The Vi accumulator itself is only populated by `DelegationStore.accumulateWitnessVi` during `MaintenanceManager.doMaintenance()`, gated behind `dynamicPropertiesStore.useNewRewardAlgorithm()`: [7](#0-6) 

and any pre-existing historical gap is only back-filled once, asynchronously, by `RewardViCalService`, which itself only activates once `getNewRewardAlgorithmEffectiveCycle()` is set on-chain and only completes after enough blocks/checkpoints have flushed: [8](#0-7) 

`VMConfig.allowTvmVote()` — the flag gating the entire `VoteRewardUtil` code path — is a separate on-chain committee proposal from the one controlling `useNewRewardAlgorithm()`/`getNewRewardAlgorithmEffectiveCycle()`. Because these two governance switches are independent, there is a governance sequencing/timing window in which:
- `allowTvmVote()` is enabled (TVM voting is live), while
- `useNewRewardAlgorithm()`/effective cycle has not been enabled, or `RewardViCalService`'s one-time backfill has not yet completed (`isDone()` false, causing `getWitnessVi` to return `BigInteger.ZERO` for all queried cycles),

In this window `VoteRewardUtil.computeReward` will compute `deltaVi = endVi - beginVi = 0` for every witness/cycle pair queried, since both values default to `BigInteger.ZERO` when unset, silently returning **zero reward** for TVM-driven voters/withdrawers even though real rewards were accrued and are correctly computable (and correctly returned) via the parallel `MortgageService` path used by ordinary account-based `VoteWitnessContract`/`WithdrawBalanceContract` transactions. This is the direct analog of the reported issue: the fee/reward accumulator is not guaranteed to be "regularly synchronized" before being consumed for a reward calculation, and the consuming code path has no fallback/staleness check, unlike its sibling implementation.

### Impact Explanation
Smart-contract-based voters (staking/voting via TVM contracts, which is a documented, supported TRON feature) can receive an incorrect (zero, or otherwise wrong if only partially backfilled) reward calculation compared to what `MortgageService` would compute for an equivalent externally-owned account performing the same votes. This constitutes a fund-miscalculation / loss-of-reward issue for a subset of on-chain participants (permanent loss of otherwise-owed reward, since `withdrawReward` also advances `beginCycle`/`endCycle` bookkeeping even when it computed zero, potentially causing the true reward for that window to become permanently unclaimable). This matches the medium-severity funds-accounting class described in the report (incorrect fee/reward distribution due to inconsistent counter state).

### Likelihood Explanation
Likelihood is dependent on chain governance sequencing (committee must enable `ALLOW_TVM_VOTE` while the new-reward-algorithm proposal/backfill is not yet complete, or historical corner cases where cycles queried fall outside what `RewardViCalService`/`DelegationStore.accumulateWitnessVi` has covered). This is realistically achievable on any network where these two proposals are activated independently/asynchronously (which is standard operational practice — proposals are typically enabled one at a time), and requires no special privilege from the attacker/victim beyond deploying/using a normal voting smart contract and calling `vote`/`withdrawreward`. This is a plausible but governance-timing-dependent scenario, so likelihood is assessed as medium rather than high.

### Recommendation
Make `VoteRewardUtil.computeReward` mirror `MortgageService.computeReward`'s dual-mode logic: check `dynamicPropertiesStore.getNewRewardAlgorithmEffectiveCycle()` and use the legacy per-cycle vote-rate reward computation (or explicitly block/revert TVM reward operations) for any cycle range that predates Vi availability, instead of unconditionally trusting `getWitnessVi` to be populated. Additionally, consider unifying the two reward-computation implementations into one shared code path to prevent this class of drift between the account-actuator and TVM-native-contract reward logic in the future.

### Proof of Concept
1. On a test network, do not activate `ALLOW_NEW_REWARD_ALGORITHM` (leave `getNewRewardAlgorithmEffectiveCycle()` unset / `Long.MAX_VALUE`), but activate `ALLOW_TVM_VOTE`.
2. Deploy a contract, freeze balance for resources (via `freezebalancecontract`), and call `vote` on one or more witnesses through the contract (routes to `VoteWitnessProcessor`).
3. Let several cycles pass with block/tx fee rewards accruing to the voted witnesses via `MortgageService.payReward` (this only populates `delegationStore.addReward`, not `WitnessVi`, since `useNewRewardAlgorithm()` is false so `MaintenanceManager` never calls `accumulateWitnessVi`).
4. Call `withdrawreward` through the contract (`WithdrawRewardProcessor.execute` → `VoteRewardUtil.withdrawReward` → `computeReward`).
5. Observe that `computeReward` reads `getWitnessVi(beginCycle-1,...)`/`getWitnessVi(endCycle-1,...)` which both return `BigInteger.ZERO` (never written), yielding `deltaVi = 0` and thus `allowance += 0`, even though `delegationStore.getReward(cycle, srAddress)` shows nonzero accrued reward for those cycles — reward that `MortgageService.queryReward`/`withdrawReward` for an equivalent EOA voter (same votes, same cycles) would correctly compute via `getOldReward`.

### Citations

**File:** chainbase/src/main/java/org/tron/core/service/MortgageService.java (L199-214)
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
```

**File:** chainbase/src/main/java/org/tron/core/service/MortgageService.java (L260-269)
```java
  private long getOldReward(long begin, long end, List<Pair<byte[], Long>> votes) {
    if (dynamicPropertiesStore.allowOldRewardOpt()) {
      return rewardViCalService.getNewRewardAlgorithmReward(begin, end, votes);
    }
    long reward = 0;
    for (long cycle = begin; cycle < end; cycle++) {
      reward += computeReward(cycle, votes);
    }
    return reward;
  }
```

**File:** actuator/src/main/java/org/tron/core/vm/utils/VoteRewardUtil.java (L90-110)
```java
  private static long computeReward(long beginCycle, long endCycle,
                                    AccountCapsule accountCapsule, Repository repository) {
    if (beginCycle >= endCycle) {
      return 0;
    }

    long reward = 0;
    for (Protocol.Vote vote : accountCapsule.getVotesList()) {
      byte[] srAddress = vote.getVoteAddress().toByteArray();
      BigInteger beginVi = repository.getDelegationStore().getWitnessVi(beginCycle - 1, srAddress);
      BigInteger endVi = repository.getDelegationStore().getWitnessVi(endCycle - 1, srAddress);
      BigInteger deltaVi = endVi.subtract(beginVi);
      if (deltaVi.signum() <= 0) {
        continue;
      }
      long userVote = vote.getVoteCount();
      reward += deltaVi.multiply(BigInteger.valueOf(userVote))
          .divide(DelegationStore.DECIMAL_OF_VI_REWARD).longValue();
    }
    return reward;
  }
```

**File:** actuator/src/main/java/org/tron/core/vm/nativecontract/VoteWitnessProcessor.java (L39-41)
```java
  public void execute(VoteWitnessParam param, Repository repo) throws ContractExeException {
    byte[] ownerAddress = param.getVoterAddress();
    VoteRewardUtil.withdrawReward(ownerAddress, repo);
```

**File:** actuator/src/main/java/org/tron/core/vm/nativecontract/WithdrawRewardProcessor.java (L38-41)
```java
  public long execute(WithdrawRewardParam param, Repository repo) throws ContractExeException {
    byte[] ownerAddress = param.getOwnerAddress();

    VoteRewardUtil.withdrawReward(ownerAddress, repo);
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

**File:** consensus/src/main/java/org/tron/consensus/dpos/MaintenanceManager.java (L94-101)
```java
    DynamicPropertiesStore dynamicPropertiesStore = consensusDelegate.getDynamicPropertiesStore();
    DelegationStore delegationStore = consensusDelegate.getDelegationStore();
    if (dynamicPropertiesStore.useNewRewardAlgorithm()) {
      long curCycle = dynamicPropertiesStore.getCurrentCycleNumber();
      consensusDelegate.getAllWitnesses().forEach(witness -> {
        delegationStore.accumulateWitnessVi(curCycle, witness.createDbKey(), witness.getVoteCount());
      });
    }
```

**File:** chainbase/src/main/java/org/tron/core/service/RewardViCalService.java (L77-127)
```java
  public void init() {
    // after init, we can get the latest block header number from db
    this.newRewardCalStartCycle = this.getNewRewardAlgorithmEffectiveCycle();
    boolean ret = this.newRewardCalStartCycle != Long.MAX_VALUE;
    if (ret) {
      // checkpoint is flushed to db, we can start rewardViCalService immediately
      lastBlockNumber = Long.MAX_VALUE;
    }
    ExecutorServiceManager.scheduleWithFixedDelay(es, this::maybeRun, 0, 3, TimeUnit.SECONDS);
  }

  private boolean enableNewRewardAlgorithm() {
    this.newRewardCalStartCycle = this.getNewRewardAlgorithmEffectiveCycle();
    boolean ret = this.newRewardCalStartCycle != Long.MAX_VALUE;
    if (ret && lastBlockNumber == -1) {
      lastBlockNumber = this.getLatestBlockHeaderNumber();
    }
    return ret;
  }

  private boolean isDone() {
    return rewardViStore.has(IS_DONE_KEY);
  }

  private void maybeRun() {
    try {
      if (enableNewRewardAlgorithm()) {
        if (this.newRewardCalStartCycle > 1) {
          if (isDone()) {
            this.clearUp(true);
            logger.info("rewardViCalService is already done");
          } else {
            if (lastBlockNumber ==  Long.MAX_VALUE // start rewardViCalService immediately
                || this.getLatestBlockHeaderNumber() > lastBlockNumber) {
              // checkpoint is flushed to db, so we can start rewardViCalService
              startRewardCal();
              clearUp(true);
            } else {
              logger.info("startRewardCal will run after checkpoint is flushed to db");
            }
          }
        } else {
          clearUp(false);
          logger.info("rewardViCalService is no need to run");
        }
      }
    } catch (Exception e) {
      logger.error(" Find fatal error, program will be exited soon.", e);
      throw new TronError(e, TronError.ErrCode.REWARD_VI_CALCULATOR);
    }
  }
```
