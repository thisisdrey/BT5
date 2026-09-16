### Title
Missing `endCycle` update in `MortgageService.withdrawReward` when votes become empty causes permanent loss of the pending cycle's vote reward - (File: `chainbase/src/main/java/org/tron/core/service/MortgageService.java`)

### Summary
`MortgageService.withdrawReward` tracks a per-account `beginCycle`/`endCycle` pair in `DelegationStore` to know which reward snapshot is still pending collection. When an account's vote list becomes empty (e.g. after unvoting all witnesses), the method updates only `beginCycle` and returns, but forgets to also update `endCycle`, unlike the symmetric non-empty-vote branch that always updates both. This desynchronizes the `beginCycle`/`endCycle` invariant, permanently disabling the "pending latest cycle" reward-snapshot lookup for that account, so a chunk of already-earned vote reward becomes unclaimable, mirroring the reported Basket `handleFees` bug where a state timestamp/cursor is not refreshed when the tracked quantity drops to zero.

### Finding Description
`MortgageService.withdrawReward(byte[] address)` [1](#0-0)  maintains `beginCycle`/`endCycle` via `DelegationStore` to represent the range of cycles whose reward has not yet been credited to the account's allowance. The normal (non-empty vote) path always keeps the invariant `beginCycle` and `endCycle` consistent by updating both at the end:
```
delegationStore.setBeginCycle(address, endCycle);
delegationStore.setEndCycle(address, endCycle + 1);
delegationStore.setAccountVote(endCycle, address, accountCapsule);
``` [2](#0-1) 

However, when the account's current vote list is empty (analogous to the Basket's `startSupply == 0` branch), the code takes a shortcut that updates only `beginCycle`:
```
if (CollectionUtils.isEmpty(accountCapsule.getVotesList())) {
  delegationStore.setBeginCycle(address, endCycle + 1);
  return;
}
``` [3](#0-2) 

`endCycle` in the store is left at its old (now stale/small) value. This breaks the invariant that `getBeginCycle(address) + 1 == getEndCycle(address)`, which is relied upon later to detect and collect a single pending reward snapshot recorded via `delegationStore.getAccountVote(beginCycle, address)`:
```
if (beginCycle + 1 == endCycle && beginCycle < currentCycle) {
  AccountCapsule account = delegationStore.getAccountVote(beginCycle, address);
  if (account != null) {
    reward = computeReward(beginCycle, endCycle, account);
    adjustAllowance(address, reward);
    ...
``` [4](#0-3) 

Once `beginCycle` is advanced past the stale `endCycle`, the condition `beginCycle + 1 == endCycle` can never become true again for that account (since `beginCycle` only grows forward while `endCycle` stays at the old, smaller value), so any snapshot reward that would have been picked up through this branch is permanently skipped. The identical `withdrawReward` logic (and the same asymmetric empty-vote shortcut) is duplicated in the TVM-reachable `VoteRewardUtil.withdrawReward` [5](#0-4) , used by the `withdrawReward` TVM opcode [6](#0-5) , and by `WithdrawRewardProcessor.execute` [7](#0-6) .

This is reached by ordinary, unprivileged flows: `VoteWitnessActuator.countVoteAccount` calls `mortgageService.withdrawReward(ownerAddress)` before clearing/re-adding the account's votes [8](#0-7) , and `WithdrawBalanceActuator`/`UnfreezeBalanceV2Actuator` call it directly on ordinary transactions [9](#0-8) [10](#0-9) .

### Impact Explanation
The bug does not create unbacked balance or allow theft of other users' funds — it only causes the affected account to permanently lose the ability to collect one cycle's worth of already-earned voting reward once its vote list transitions from empty back to non-empty. This is a bounded but genuine, reproducible loss/freezing of a user's own funds triggered entirely by their own ordinary unvote → revote sequence, without any attacker interaction, so severity is limited relative to the original Basket finding (which caused systemic dilution affecting all basket holders on every resupply after an emptying event).

### Likelihood Explanation
Any account can trigger this by submitting a `VoteWitnessContract` with an empty vote list (unvote) at a moment where `beginCycle + 1 == endCycle` and later re-voting — a completely normal user action requiring no special permission, timing precision beyond ordinary cycle boundaries, or contract deployment.

### Recommendation
In the empty-votes branch of `MortgageService.withdrawReward` (and the duplicated `VoteRewardUtil.withdrawReward`), keep `beginCycle`/`endCycle` consistent the same way the non-empty branch does, e.g. also call `delegationStore.setEndCycle(address, endCycle + 1)` (and consider still calling `computeReward`/`adjustAllowance` for the pending snapshot before returning), so a subsequent re-vote does not silently drop an already-accrued reward snapshot.

### Proof of Concept
1. Account A votes for a witness such that after normal cycle progression the stored state satisfies `beginCycle + 1 == endCycle` (the routine invariant maintained by the non-empty path).
2. Account A submits `VoteWitnessContract` with an empty vote list, which invokes `VoteWitnessActuator.countVoteAccount` → `mortgageService.withdrawReward(A)`; because `getVotesList()` is empty, only `setBeginCycle(A, endCycle+1)` runs, leaving `endCycle` stale in `DelegationStore`.
3. Several cycles later, Account A votes again for a witness. `withdrawReward` is invoked again with the new (advanced) `beginCycle` and the still-stale `endCycle`; the condition `beginCycle + 1 == endCycle` is now false, so the branch that would fetch `delegationStore.getAccountVote(beginCycle, A)` and add its reward via `adjustAllowance` never executes for that snapshot.
4. The reward corresponding to that snapshot cycle is never credited to Account A's allowance and cannot be recovered by any future call, since the invariant needed to reach that code path is permanently broken for this account.

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

**File:** actuator/src/main/java/org/tron/core/vm/utils/VoteRewardUtil.java (L44-47)
```java
    if (CollectionUtils.isEmpty(accountCapsule.getVotesList())) {
      repository.updateBeginCycle(address, endCycle + 1);
      return;
    }
```

**File:** actuator/src/main/java/org/tron/core/vm/program/Program.java (L2340-2369)
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
    } catch (ContractValidateException e) {
      logger.warn("TVM WithdrawReward: validate failure. Reason: {}", e.getMessage());
    } catch (ContractExeException e) {
      logger.warn("TVM WithdrawReward: execute failure. Reason: {}", e.getMessage());
    }
    if (internalTx != null) {
      internalTx.reject();
    }
    return 0;
  }
```

**File:** actuator/src/main/java/org/tron/core/vm/nativecontract/WithdrawRewardProcessor.java (L38-68)
```java
  public long execute(WithdrawRewardParam param, Repository repo) throws ContractExeException {
    byte[] ownerAddress = param.getOwnerAddress();

    VoteRewardUtil.withdrawReward(ownerAddress, repo);

    AccountCapsule accountCapsule = repo.getAccount(ownerAddress);
    long oldBalance = accountCapsule.getBalance();
    long allowance = accountCapsule.getAllowance();
    long newBalance = 0;

    try {
      newBalance = LongMath.checkedAdd(oldBalance, allowance);
    } catch (ArithmeticException e) {
      logger.debug(e.getMessage(), e);
      throw new ContractExeException(e.getMessage());
    }

    // If no allowance, do nothing and just return zero.
    if (allowance <= 0) {
      return 0;
    }

    accountCapsule.setInstance(accountCapsule.getInstance().toBuilder()
        .setBalance(newBalance)
        .setAllowance(0L)
        .setLatestWithdrawTime(param.getNowInMs())
        .build());

    repo.updateAccount(accountCapsule.createDbKey(), accountCapsule);
    return allowance;
  }
```

**File:** actuator/src/main/java/org/tron/core/actuator/VoteWitnessActuator.java (L152-191)
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

    DynamicPropertiesStore dynamicStore = chainBaseManager.getDynamicPropertiesStore();
    if (dynamicStore.supportAllowNewResourceModel()
        && accountCapsule.oldTronPowerIsNotInitialized()) {
      accountCapsule.initializeOldTronPower();
    }

    if (!votesStore.has(ownerAddress)) {
      votesCapsule = new VotesCapsule(voteContract.getOwnerAddress(),
          accountCapsule.getVotesList());
    } else {
      votesCapsule = votesStore.get(ownerAddress);
    }

    accountCapsule.clearVotes();
    votesCapsule.clearNewVotes();

    voteContract.getVotesList().forEach(vote -> {
      logger.debug("countVoteAccount, address[{}]",
          ByteArray.toHexString(vote.getVoteAddress().toByteArray()));

      votesCapsule.addNewVotes(vote.getVoteAddress(), vote.getVoteCount());
      accountCapsule.addVotes(vote.getVoteAddress(), vote.getVoteCount());
    });

    accountStore.put(accountCapsule.createDbKey(), accountCapsule);
    votesStore.put(ownerAddress, votesCapsule);
  }
```

**File:** actuator/src/main/java/org/tron/core/actuator/WithdrawBalanceActuator.java (L46-56)
```java
    try {
      withdrawBalanceContract = any.unpack(WithdrawBalanceContract.class);
    } catch (InvalidProtocolBufferException e) {
      logger.debug(e.getMessage(), e);
      ret.setStatus(fee, code.FAILED);
      throw new ContractExeException(e.getMessage());
    }

    mortgageService.withdrawReward(withdrawBalanceContract.getOwnerAddress()
        .toByteArray());

```

**File:** actuator/src/main/java/org/tron/core/actuator/UnfreezeBalanceV2Actuator.java (L69-72)
```java
    byte[] ownerAddress = unfreezeBalanceV2Contract.getOwnerAddress().toByteArray();
    long now = dynamicStore.getLatestBlockHeaderTimestamp();

    mortgageService.withdrawReward(ownerAddress);
```
