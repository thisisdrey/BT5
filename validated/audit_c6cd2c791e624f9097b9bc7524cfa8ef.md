### Title
TVM `voteWitness` precompile skips `initializeOldTronPower` sync, letting contract calls vote with stale/incorrect TronPower - (File: `actuator/src/main/java/org/tron/core/vm/nativecontract/VoteWitnessProcessor.java`)

### Summary
The reported Malt bug is a "use derived/aggregate data before it has been synchronized" class of bug: `StabilizerNode.stabilize` reads `globalIC.collateralRatio()` without first calling `RewardThrottle.checkRewardUnderflow()` / `impliedCollateralService.syncGlobalCollateral()`, so a stale collateral figure feeds a critical price/ratio calculation. The closest reachable analog in java-tron is the divergence between the normal `VoteWitnessContract` actuator path and the TVM `voteWitness` opcode path: both compute a `tronPower` ceiling used to validate that the sum of votes does not exceed the caller's power, but only one of the two paths performs the required state-synchronization step first.

### Finding Description
In the ordinary transaction path, `VoteWitnessActuator.countVoteAccount` explicitly synchronizes the account's power-accounting state before votes are recorded: [1](#0-0) 

This lazily migrates the account onto the "new resource model" power accounting (`oldTronPowerIsNotInitialized()` / `initializeOldTronPower()`) before `accountCapsule.getAllTronPower()` is later relied upon (e.g. in `validate()`): [2](#0-1) 

The TVM-reachable equivalent, `VoteWitnessProcessor.execute` (invoked from the `voteWitness` TVM opcode handler in `OperationActions.voteWitnessAction` → `Program.voteWitness`), computes the same `tronPower` ceiling and performs the same vote-sum-vs-power check, but never calls `initializeOldTronPower()`: [3](#0-2) [4](#0-3) 

`VoteRewardUtil.withdrawReward` is called first (mirroring `mortgageService.withdrawReward` in the actuator path), but the power-accounting migration step is entirely omitted: [5](#0-4) 

Because any deployed smart contract can invoke the `voteWitness` opcode, this is a path reachable by an unprivileged contract deployer/caller, exactly the class of "critical calculation performed on un-synchronized state" that the Malt finding flags — just as `collateralRatio()` was computed before `checkRewardUnderflow`/`syncGlobalCollateral` moved undistributed rewards into the accounted total, here `getAllTronPower()` is computed before the account's power-accounting fields are migrated/synchronized to the new model.

### Impact Explanation
If an account has never gone through the actuator-based vote/freeze/unfreeze flow that performs the migration, calling `voteWitness` via the TVM path uses whatever default/uninitialized value the "old" tron-power field holds, producing a `tronPower` figure that is inconsistent with the value the same account would get through the normal actuator path (or in a subsequent maintenance cycle once some other path finally performs the migration). This state divergence between the two code paths that are both supposed to enforce "votes ≤ tronPower" can let a contract-originated vote transaction pass the check with a `tronPower` value that does not reflect the account's true committed stake, and it also leaves the account's persisted state inconsistent (the field is written by one path but not synchronized by the other), which can silently distort subsequent power calculations and validation for that same account across both entry points — analogous to the collateral ratio being computed on unsynchronized data and downstream decisions (pricing, in the Malt case; witness voting power, here) being made incorrectly.

### Likelihood Explanation
Low-to-moderate: it requires an account whose `oldTronPower` field has not yet been initialized by the legacy actuator path to instead call the `voteWitness` precompile from a contract while `supportUnfreezeDelay()` and `supportAllowNewResourceModel()` are both active. This is a realistic but not universal scenario (new-resource-model accounts, or accounts freezing/voting exclusively through contracts). As with the original finding, the magnitude of the resulting discrepancy is likely to be small in most cases but the root cause — a missing synchronization step before a critical power-ratio validation — mirrors the reported bug class.

### Recommendation
Have `VoteWitnessProcessor.execute` perform the same account-power migration as `VoteWitnessActuator.countVoteAccount` before reading `getAllTronPower()`/`getTronPower()`:
```java
if (repo.getDynamicPropertiesStore().supportAllowNewResourceModel()
    && accountCapsule.oldTronPowerIsNotInitialized()) {
  accountCapsule.initializeOldTronPower();
}
```
placed immediately after `VoteRewardUtil.withdrawReward(...)` and before the `tronPower` computation, ensuring both entry points (actuator and TVM opcode) synchronize account power state identically before it is used in the vote-validation calculation.

### Proof of Concept
1. Create/select an account that has never been processed by `VoteWitnessActuator`/`FreezeBalanceActuator` migration logic, so its `oldTronPower`-related field is left at its default (not yet initialized) while `supportAllowNewResourceModel()` and `supportUnfreezeDelay()` are enabled on-chain.
2. Deploy a contract and invoke the `voteWitness` TVM opcode (`OperationActions.voteWitnessAction` → `Program.voteWitness` → `VoteWitnessProcessor.execute`) for that account, submitting votes sized against the un-migrated `getAllTronPower()` value.
3. Compare against calling `VoteWitnessActuator` (regular transaction) for the same account state, which performs `initializeOldTronPower()` first and can yield a different `tronPower` ceiling.
4. Observe that the vote-count-vs-tronPower check in the two paths can be satisfied against differing `tronPower` baselines for the identical underlying account state, demonstrating the synchronization gap described in `VoteWitnessProcessor.java` lines 88-100 versus `VoteWitnessActuator.java` lines 165-169.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/VoteWitnessActuator.java (L129-143)
```java
      long tronPower;
      DynamicPropertiesStore dynamicStore = chainBaseManager.getDynamicPropertiesStore();
      if (dynamicStore.supportAllowNewResourceModel()) {
        tronPower = accountCapsule.getAllTronPower();
      } else {
        tronPower = accountCapsule.getTronPower();
      }

      sum = LongMath
          .checkedMultiply(sum, TRX_PRECISION); //trx -> drop. The vote count is based on TRX
      if (sum > tronPower) {
        throw new ContractValidateException(
            "The total number of votes[" + sum + "] is greater than the tronPower[" + tronPower
                + "]");
      }
```

**File:** actuator/src/main/java/org/tron/core/actuator/VoteWitnessActuator.java (L165-169)
```java
    DynamicPropertiesStore dynamicStore = chainBaseManager.getDynamicPropertiesStore();
    if (dynamicStore.supportAllowNewResourceModel()
        && accountCapsule.oldTronPowerIsNotInitialized()) {
      accountCapsule.initializeOldTronPower();
    }
```

**File:** actuator/src/main/java/org/tron/core/vm/nativecontract/VoteWitnessProcessor.java (L39-100)
```java
  public void execute(VoteWitnessParam param, Repository repo) throws ContractExeException {
    byte[] ownerAddress = param.getVoterAddress();
    VoteRewardUtil.withdrawReward(ownerAddress, repo);

    AccountCapsule accountCapsule = repo.getAccount(ownerAddress);

    VotesCapsule votesCapsule = repo.getVotes(ownerAddress);
    if (votesCapsule == null) {
      votesCapsule = new VotesCapsule(ByteString.copyFrom(ownerAddress),
          accountCapsule.getVotesList());
    }

    accountCapsule.clearVotes();
    votesCapsule.clearNewVotes();

    Map<ByteString, Long> voteMap = new HashMap<>();
    Iterator<Protocol.Vote> iterator = param.getVotes().iterator();
    try {
      long sum = 0;
      while (iterator.hasNext()) {
        Protocol.Vote vote = iterator.next();

        byte[] witnessAddress = vote.getVoteAddress().toByteArray();
        /*
          Already covered while doing maintenance in MaintenanceManager.java, for tvm performance,
          we remove the account check
         */
//        if (repo.getAccount(witnessAddress) == null) {
//          throw new ContractValidateException(
//              ACCOUNT_EXCEPTION_STR + StringUtil.encode58Check(witnessAddress) + NOT_EXIST_STR);
//        }
        if (repo.getWitness(witnessAddress) == null) {
          throw new ContractExeException(
              WITNESS_EXCEPTION_STR + StringUtil.encode58Check(witnessAddress) + NOT_EXIST_STR);
        }

        long voteCount = vote.getVoteCount();
        if (voteCount < 0) {
          throw new ContractExeException("Vote count must not be less than 0");
        } else if (voteCount == 0) {
          iterator.remove();
        } else {
          sum = LongMath.checkedAdd(sum, voteCount);
          // merge vote for same witness
          voteMap.put(vote.getVoteAddress(),
              LongMath.checkedAdd(voteMap.getOrDefault(vote.getVoteAddress(), 0L), voteCount));
        }
      }

      long tronPower;
      if (repo.getDynamicPropertiesStore().supportUnfreezeDelay()
          && repo.getDynamicPropertiesStore().supportAllowNewResourceModel()) {
        tronPower = accountCapsule.getAllTronPower();
      } else {
        tronPower = accountCapsule.getTronPower();
      }
      sum =  LongMath.checkedMultiply(sum, TRX_PRECISION);
      if (sum > tronPower) {
        throw new ContractExeException(
            "The total number of votes[" + sum + "] is greater than the tronPower[" + tronPower
                + "]");
      }
```

**File:** actuator/src/main/java/org/tron/core/vm/OperationActions.java (L905-919)
```java
  public static void voteWitnessAction(Program program) {
    if (program.isStaticCall()) {
      throw new Program.StaticCallModificationException();
    }

    int amountArrayLength = program.stackPop().intValueSafe();
    int amountArrayOffset = program.stackPop().intValueSafe();
    int witnessArrayLength = program.stackPop().intValueSafe();
    int witnessArrayOffset = program.stackPop().intValueSafe();

    boolean result = program.voteWitness(witnessArrayOffset, witnessArrayLength,
        amountArrayOffset, amountArrayLength);
    program.stackPush(result ? DataWord.ONE() : DataWord.ZERO());
    program.step();
  }
```

**File:** actuator/src/main/java/org/tron/core/vm/utils/VoteRewardUtil.java (L16-55)
```java
  public static void withdrawReward(byte[] address, Repository repository) {
    if (!VMConfig.allowTvmVote()) {
      return;
    }
    AccountCapsule accountCapsule = repository.getAccount(address);
    long beginCycle = repository.getBeginCycle(address);
    long endCycle = repository.getEndCycle(address);
    long currentCycle = repository.getDynamicPropertiesStore().getCurrentCycleNumber();
    long reward = 0;
    if (beginCycle > currentCycle || accountCapsule == null) {
      return;
    }
    if (beginCycle == currentCycle) {
      AccountCapsule account = repository.getAccountVote(beginCycle, address);
      if (account != null) {
        return;
      }
    }
    if (beginCycle + 1 == endCycle && beginCycle < currentCycle) {
      AccountCapsule account = repository.getAccountVote(beginCycle, address);
      if (account != null) {
        reward = computeReward(beginCycle, endCycle, account, repository);
        adjustAllowance(address, reward, repository);
        reward = 0;
      }
      beginCycle += 1;
    }
    endCycle = currentCycle;
    if (CollectionUtils.isEmpty(accountCapsule.getVotesList())) {
      repository.updateBeginCycle(address, endCycle + 1);
      return;
    }
    if (beginCycle < endCycle) {
      reward += computeReward(beginCycle, endCycle, accountCapsule, repository);
      adjustAllowance(address, reward, repository);
    }
    repository.updateBeginCycle(address, endCycle);
    repository.updateEndCycle(address, endCycle + 1);
    repository.updateAccountVote(address, endCycle, accountCapsule);
  }
```
