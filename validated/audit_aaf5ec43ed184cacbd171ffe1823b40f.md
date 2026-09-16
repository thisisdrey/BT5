### Title
Legacy TVM `unfreezebalance` opcode forcibly clears votes using stale `getTronPower()` instead of `getAllTronPower()`, unfairly punishing accounts under the new resource model - ([File: actuator/src/main/java/org/tron/core/vm/nativecontract/UnfreezeBalanceProcessor.java])

### Summary
The original report describes a valuation function (`WCurveGauge#pendingRewards`) that fails to account for a value component (pending rewards) that is later used by a health/liquidation check, causing an unfair penalizing action (liquidation) against users who are actually safe. The closest reachable analog in java-tron is `UnfreezeBalanceProcessor.execute()`, which decides whether to forcibly clear a user's votes based on an incomplete power calculation that ignores a value component (`oldTronPower`) that the "new resource model" introduced specifically to avoid this class of bug.

### Finding Description
`UnfreezeBalanceProcessor.execute()` is the TVM native-contract implementation of the legacy `UnfreezeBalance` opcode, reachable from any contract that still has old-style frozen balance and calls the TVM `unfreezeBalance()` precompile/opcode.

After moving balance back to the account, it checks whether the account's remaining "tron power" can still support its outstanding votes: [1](#0-0) 

The check uses `accountCapsule.getTronPower()`, which only reflects the *old* frozen-balance model: [2](#0-1) 

However, the codebase elsewhere (in every other actuator/processor that performs the identical "does user still have enough power to support votes" check, e.g. `VoteWitnessActuator`, `VoteWitnessProcessor`, `UnfreezeBalanceV2Actuator.updateVote`, `UnfreezeBalanceV2Processor.updateVote`) deliberately uses `getAllTronPower()` when `supportAllowNewResourceModel()`/`allowTvmVote()` is active, because `getAllTronPower()` also folds in `account.getOldTronPower()` — a value carried over from before the FreezeV2 migration: [3](#0-2) [4](#0-3) [5](#0-4) 

`UnfreezeBalanceProcessor` is the one place performing this same "power vs. used votes" comparison that was never updated to add the `oldTronPower` component. For an account that migrated to the new resource model (`oldTronPower` set to a positive value via `initializeOldTronPower()`), `getTronPower()` under-reports the account's true backing power because part of its power now lives in `account.getOldTronPower()` rather than in `FrozenV1` slots that `getTronPower()` sums. This mirrors the reported bug class exactly: a value/health computation omits a legitimate value component, and that omission feeds directly into an enforcement action (here: unilaterally clearing all of the account's votes) that should not have triggered.

### Impact Explanation
When the condition `accountCapsule.getTronPower() < usedTronPower * TRX_PRECISION` is incorrectly satisfied (because `getTronPower()` omits `oldTronPower`), the processor force-clears **all** of the account's votes and persists the empty `VotesCapsule`: [6](#0-5) 

This causes an account that genuinely still has sufficient combined tron power (old + new frozen) to have its votes wiped out without consent, exactly analogous to the reported "unfair liquidation": an enforcement action is taken against a party whose true collateral/power was undervalued due to an incomplete calculation. It also means the account permanently loses any pending vote-based reward it might have accrued between cycles for the cleared witnesses, and the witnesses lose the vote weight, potentially perturbing SR elections.

### Likelihood Explanation
This path is reachable by any account that: (1) still holds legacy `Frozen`/`AccountResource.frozenBalanceForEnergy` balances, (2) has cast votes, (3) is running under `allowTvmVote()` (TVM vote support enabled) and the new resource model, and (4) issues an `unfreezeBalance` TVM opcode call (a broadcastable, unprivileged contract call). No special privilege is required — this is directly reachable via a standard `TriggerSmartContract` transaction invoking the unfreeze precompile from an ordinary account/contract.

### Recommendation
Update `UnfreezeBalanceProcessor.execute()` to use the same power-selection logic as `VoteWitnessProcessor`/`UnfreezeBalanceV2Processor`: when `supportAllowNewResourceModel()` (or equivalently `!accountCapsule.oldTronPowerIsInvalid()`), compare against `accountCapsule.getAllTronPower()` instead of `accountCapsule.getTronPower()`, so that votes are only cleared when the account's *total* power (including migrated `oldTronPower`) is insufficient to support its votes.

### Proof of Concept
1. Enable `allowTvmVote()` and `supportAllowNewResourceModel()`.
2. Create an account, freeze balance via the legacy `FreezeBalance` path to obtain `Frozen` entries, then let it migrate so that `initializeOldTronPower()` sets a positive `account.getOldTronPower()` (as done in `VoteWitnessActuator`/`VoteWitnessProcessor` when first voting under the new model) — see the test pattern in [7](#0-6) 
3. Cast votes using tron power that is only satisfied when `getAllTronPower()` (old + new) is counted, but exceeds `getTronPower()` alone (i.e., votes ≤ `getAllTronPower()` trx but > `getTronPower()` trx).
4. From that account/contract, invoke the TVM `unfreezeBalance()` opcode (via `Program.withdrawExpireUnfreeze`-style precompile entry that routes into `UnfreezeBalanceProcessor.execute`).
5. Observe that despite the account's `getAllTronPower()` still exceeding `usedTronPower * TRX_PRECISION`, the stale `getTronPower()` check fails and all votes are wiped via `accountCapsule.clearVotes()` / `repo.updateVotes(ownerAddress, votesCapsule)`.

Note: I was unable to trace the exact TVM opcode dispatcher method name that invokes `UnfreezeBalanceProcessor` from `Program.java` within the available index; a Devin session with full repository access would be needed to confirm the precise call site and write an executable end-to-end test.

### Citations

**File:** actuator/src/main/java/org/tron/core/vm/nativecontract/UnfreezeBalanceProcessor.java (L205-223)
```java
    if (VMConfig.allowTvmVote() && !accountCapsule.getVotesList().isEmpty()) {
      long usedTronPower = 0;
      for (Protocol.Vote vote : accountCapsule.getVotesList()) {
        usedTronPower += vote.getVoteCount();
      }
      if (accountCapsule.getTronPower() < usedTronPower * TRX_PRECISION) {
        VoteRewardUtil.withdrawReward(ownerAddress, repo);
        VotesCapsule votesCapsule = repo.getVotes(ownerAddress);
        accountCapsule = repo.getAccount(ownerAddress);
        if (votesCapsule == null) {
          votesCapsule = new VotesCapsule(ByteString.copyFrom(ownerAddress),
              accountCapsule.getVotesList());
        } else {
          votesCapsule.clearNewVotes();
        }
        accountCapsule.clearVotes();
        repo.updateVotes(ownerAddress, votesCapsule);
        repo.updateAccount(ownerAddress, accountCapsule);
      }
```

**File:** chainbase/src/main/java/org/tron/core/capsule/AccountCapsule.java (L619-635)
```java
  //tp:Tron_Power
  public long getTronPower() {
    long tp = 0;
    for (int i = 0; i < account.getFrozenCount(); ++i) {
      tp += account.getFrozen(i).getFrozenBalance();
    }

    tp += account.getAccountResource().getFrozenBalanceForEnergy().getFrozenBalance();
    tp += account.getDelegatedFrozenBalanceForBandwidth();
    tp += account.getAccountResource().getDelegatedFrozenBalanceForEnergy();

    tp += getFrozenV2List().stream().filter(o -> o.getType() != TRON_POWER)
            .mapToLong(FreezeV2::getAmount).sum();
    tp += account.getDelegatedFrozenV2BalanceForBandwidth();
    tp += account.getAccountResource().getDelegatedFrozenV2BalanceForEnergy();
    return tp;
  }
```

**File:** chainbase/src/main/java/org/tron/core/capsule/AccountCapsule.java (L637-646)
```java
  public long getAllTronPower() {
    if (account.getOldTronPower() == -1) {
      return getTronPowerFrozenBalance() + getTronPowerFrozenV2Balance();
    } else if (account.getOldTronPower() == 0) {
      return getTronPower() + getTronPowerFrozenBalance() + getTronPowerFrozenV2Balance();
    } else {
      return account.getOldTronPower() + getTronPowerFrozenBalance()
          + getTronPowerFrozenV2Balance();
    }
  }
```

**File:** actuator/src/main/java/org/tron/core/vm/nativecontract/VoteWitnessProcessor.java (L88-100)
```java
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

**File:** actuator/src/main/java/org/tron/core/vm/nativecontract/UnfreezeBalanceV2Processor.java (L254-263)
```java
    long ownedTronPower;
    if (dynamicStore.supportAllowNewResourceModel()) {
      ownedTronPower = accountCapsule.getAllTronPower();
    } else {
      ownedTronPower = accountCapsule.getTronPower();
    }
    // tron power is enough to total votes
    if (ownedTronPower >= totalVote * TRX_PRECISION) {
      return;
    }
```

**File:** framework/src/test/java/org/tron/core/actuator/VoteWitnessActuatorTest.java (L608-639)
```java
  @Test
  public void voteWitnessWithOldAndNewTronPowerAfterNewResourceModel() {

    dbManager.getDynamicPropertiesStore().saveAllowNewResourceModel(1L);

    AccountCapsule owner =
        dbManager.getAccountStore().get(ByteArray.fromHexString(OWNER_ADDRESS));
    owner.setFrozenForEnergy(2000000L,0L);
    owner.setFrozenForTronPower(1000000L,0L);
    dbManager.getAccountStore().put(ByteArray.fromHexString(OWNER_ADDRESS),owner);

    VoteWitnessActuator actuator = new VoteWitnessActuator();
    actuator.setChainBaseManager(dbManager.getChainBaseManager())
        .setAny(getContract(OWNER_ADDRESS, WITNESS_ADDRESS, 1L));
    TransactionResultCapsule ret = new TransactionResultCapsule();
    try {
      actuator.validate();
      actuator.execute(ret);

      owner =
          dbManager.getAccountStore().get(ByteArray.fromHexString(OWNER_ADDRESS));
      Assert.assertEquals(3000000L, owner.getAllTronPower());
      Assert.assertEquals(2000000L, owner.getInstance().getOldTronPower());
      Assert.assertEquals(1000000L, owner.getInstance().getTronPower().getFrozenBalance());
    } catch (ContractValidateException e) {
      e.printStackTrace();
      Assert.assertFalse(e instanceof ContractValidateException);
    } catch (ContractExeException e) {
      Assert.assertFalse(e instanceof ContractExeException);
    }
    dbManager.getDynamicPropertiesStore().saveAllowNewResourceModel(0L);
  }
```
