### Title
DelegateResourceActuator/DelegateResourceProcessor reduce an account's frozen (TronPower-backing) balance without withdrawing/settling vote rewards or adjusting the existing votes list, unlike the equivalent Unfreeze paths - ([File: actuator/src/main/java/org/tron/core/actuator/DelegateResourceActuator.java], [File: actuator/src/main/java/org/tron/core/vm/nativecontract/DelegateResourceProcessor.java])

### Summary
The Sherlock finding describes `Lender._transfer` moving/updating a balance that backs reward accounting (shares) without first accruing interest for the affected address (`RESERVE`), so the reward-state update operates on a stale balance and rewards are mis-accrued. The closest reachable analog in java-tron is `DelegateResourceActuator.execute` / `DelegateResourceProcessor.execute`, which reduce an owner's `FrozenV2BalanceForBandwidth`/`FrozenV2BalanceForEnergy` (the balance that backs `TronPower`, which in turn backs cast votes and vote-reward accrual) without calling `MortgageService.withdrawReward` / `VoteRewardUtil.withdrawReward` and without adjusting/clearing the account's `votes` list, unlike every other actuator that changes an account's TronPower-backing balance.

### Finding Description
Vote rewards in java-tron are computed per voting cycle from the account's recorded `votes` list via `MortgageService.withdrawReward` / `VoteRewardUtil.withdrawReward`, which snapshot and settle rewards for the current cycle's votes before any change is made to the state that determines how many votes an account is entitled to keep.

Every other actuator/processor that reduces the TronPower-backing frozen balance follows the pattern "settle reward for current votes, then reduce balance, then trim/clear votes to match new TronPower":
- `UnfreezeBalanceV2Actuator.execute` calls `mortgageService.withdrawReward(ownerAddress)` [1](#0-0)  before reducing the frozen balance and then calls `updateVote(...)` to clear/trim the account's votes so they don't exceed the new TronPower [2](#0-1) .
- `UnfreezeBalanceProcessor.execute` (TVM path) likewise calls `VoteRewardUtil.withdrawReward` and clears votes when TronPower drops below used votes [3](#0-2) .
- `VoteWitnessActuator.countVoteAccount` calls `mortgageService.withdrawReward(ownerAddress)` before clearing/re-adding votes [4](#0-3) .

In contrast, `DelegateResourceActuator.execute` reduces `FrozenBalanceForBandwidthV2`/`FrozenBalanceForEnergyV2` for the owner (moving it into `DelegatedFrozenV2Balance...`) with no call to `withdrawReward` and no adjustment of the owner's `votes` list at all: [5](#0-4) . The same omission exists in the TVM-reachable `DelegateResourceProcessor.execute`, which performs the identical frozen-balance reduction with no reward settlement or vote adjustment: [6](#0-5) .

Because `votes` are left untouched, an account can delegate away the frozen balance that backs its existing votes while continuing to be counted (in `MortgageService.computeReward`/`VoteRewardUtil.computeReward`) as having cast the full vote amount for subsequent cycles, since those functions only read the stored `votes` list and per-witness `Vi` deltas — they never re-check that the account's current TronPower still covers the vote count. This is the root-cause analog of the Sherlock issue: a state-changing operation on the reward-relevant balance proceeds without first settling/adjusting the reward-relevant account state (votes) for the address whose balance changed.

### Impact Explanation
An account can call `DelegateResourceContract` (or the TVM `delegateResource` opcode) to move its frozen balance to another receiver as delegated resource while keeping its full vote allocation uncorrected. Because reward computation (`computeReward` in `MortgageService`/`VoteRewardUtil`) is driven purely by the stored `vote.getVoteCount()` per witness and the witness's `Vi` delta, not by the voter's current TronPower, the voter keeps earning full vote rewards for votes that are no longer backed by owned (undelegated) TronPower. This lets a user earn vote rewards disproportionate to their real staked TronPower, which is a form of unbacked/incorrectly-accounted reward accrual — directly analogous to the RESERVE losing/gaining incorrect rewards due to a stale balance in the referenced report, but here it benefits the attacker rather than costing the special account, making it Medium/High severity depending on scale of exploitation.

### Likelihood Explanation
`DelegateResourceContract` is a normal, unprivileged, broadcastable transaction type (also reachable via the TVM `delegateResource` precompiled opcode from any contract), requiring no special permissions — any account holding frozen balance and existing votes can trigger this path repeatedly across cycles, making the likelihood high for any account that both votes and later delegates resources.

### Recommendation
Mirror the pattern used in `UnfreezeBalanceV2Actuator`/`UnfreezeBalanceProcessor`: before reducing `FrozenBalanceForBandwidthV2`/`FrozenBalanceForEnergyV2` in `DelegateResourceActuator.execute` and `DelegateResourceProcessor.execute`, call `mortgageService.withdrawReward` (or `VoteRewardUtil.withdrawReward` in the TVM path) to settle current-cycle rewards for the owner, then check whether the owner's remaining TronPower still covers `sum(votes)`, and if not, clear/trim the `votes` list accordingly (as done in `updateVote`).

### Proof of Concept
1. Account A freezes balance for bandwidth, giving it TronPower `P`.
2. Account A votes for witness W with `voteCount = P` (using all TronPower), which is stored via `VoteWitnessActuator`/`VoteWitnessProcessor` (which correctly calls `withdrawReward` first).
3. Account A then submits `DelegateResourceContract` to delegate all of its frozen bandwidth balance to Account B. `DelegateResourceActuator.execute` reduces A's `FrozenBalanceForBandwidthV2` to 0 [7](#0-6) , but never touches A's `votes` list or calls `withdrawReward`.
4. At the next reward cycle, `MortgageService.withdrawReward`/`computeReward` for A still uses the full `voteCount = P` for witness W (from `accountCapsule.getVotesList()`), even though A's actual owned TronPower is now 0, because nothing ever cleared/adjusted A's votes when the underlying frozen balance was delegated away [8](#0-7) .
5. A continues to receive vote rewards for TronPower it no longer owns, while B receives the delegated resource benefits — demonstrating unbacked reward accrual reachable through a normal signed transaction.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/UnfreezeBalanceV2Actuator.java (L69-76)
```java
    byte[] ownerAddress = unfreezeBalanceV2Contract.getOwnerAddress().toByteArray();
    long now = dynamicStore.getLatestBlockHeaderTimestamp();

    mortgageService.withdrawReward(ownerAddress);

    AccountCapsule accountCapsule = accountStore.get(ownerAddress);
    long unfreezeAmount = this.unfreezeExpire(accountCapsule, now);
    long unfreezeBalance = unfreezeBalanceV2Contract.getUnfreezeBalance();
```

**File:** actuator/src/main/java/org/tron/core/actuator/UnfreezeBalanceV2Actuator.java (L303-337)
```java
  private void updateVote(AccountCapsule accountCapsule,
                          final UnfreezeBalanceV2Contract unfreezeBalanceV2Contract,
                          byte[] ownerAddress) {
    DynamicPropertiesStore dynamicStore = chainBaseManager.getDynamicPropertiesStore();
    VotesStore votesStore = chainBaseManager.getVotesStore();

    if (accountCapsule.getVotesList().isEmpty()) {
      return;
    }
    if (dynamicStore.supportAllowNewResourceModel()) {
      if (accountCapsule.oldTronPowerIsInvalid()) {
        switch (unfreezeBalanceV2Contract.getResource()) {
          case BANDWIDTH:
          case ENERGY:
            // there is no need to change votes
            return;
          default:
            break;
        }
      } else {
        // clear all votes at once when new resource model start
        VotesCapsule votesCapsule;
        if (!votesStore.has(ownerAddress)) {
          votesCapsule = new VotesCapsule(
              unfreezeBalanceV2Contract.getOwnerAddress(),
              accountCapsule.getVotesList()
          );
        } else {
          votesCapsule = votesStore.get(ownerAddress);
        }
        accountCapsule.clearVotes();
        votesCapsule.clearNewVotes();
        votesStore.put(ownerAddress, votesCapsule);
        return;
      }
```

**File:** actuator/src/main/java/org/tron/core/vm/nativecontract/UnfreezeBalanceProcessor.java (L205-224)
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
    }
```

**File:** actuator/src/main/java/org/tron/core/actuator/VoteWitnessActuator.java (L160-162)
```java
    //
    mortgageService.withdrawReward(ownerAddress);

```

**File:** actuator/src/main/java/org/tron/core/actuator/DelegateResourceActuator.java (L73-98)
```java
    // delegate resource to receiver
    switch (delegateResourceContract.getResource()) {
      case BANDWIDTH:
        delegateResource(ownerAddress, receiverAddress, true,
            delegateBalance, lock, lockPeriod);

        ownerCapsule.addDelegatedFrozenV2BalanceForBandwidth(delegateBalance);
        ownerCapsule.addFrozenBalanceForBandwidthV2(-delegateBalance);
        break;
      case ENERGY:
        delegateResource(ownerAddress, receiverAddress, false,
            delegateBalance, lock, lockPeriod);

        ownerCapsule.addDelegatedFrozenV2BalanceForEnergy(delegateBalance);
        ownerCapsule.addFrozenBalanceForEnergyV2(-delegateBalance);
        break;
      default:
        logger.debug("Resource Code Error.");
    }

    accountStore.put(ownerCapsule.createDbKey(), ownerCapsule);

    ret.setStatus(fee, code.SUCESS);

    return true;
  }
```

**File:** actuator/src/main/java/org/tron/core/vm/nativecontract/DelegateResourceProcessor.java (L117-144)
```java
  public void execute(DelegateResourceParam param, Repository repo) {
    byte[] ownerAddress = param.getOwnerAddress();
    AccountCapsule ownerCapsule = repo.getAccount(param.getOwnerAddress());
    long delegateBalance = param.getDelegateBalance();
    byte[] receiverAddress = param.getReceiverAddress();

    // delegate resource to receiver
    switch (param.getResourceType()) {
      case BANDWIDTH:
        delegateResource(ownerAddress, receiverAddress, true,
            delegateBalance, repo);

        ownerCapsule.addDelegatedFrozenV2BalanceForBandwidth(delegateBalance);
        ownerCapsule.addFrozenBalanceForBandwidthV2(-delegateBalance);
        break;
      case ENERGY:
        delegateResource(ownerAddress, receiverAddress, false,
            delegateBalance, repo);

        ownerCapsule.addDelegatedFrozenV2BalanceForEnergy(delegateBalance);
        ownerCapsule.addFrozenBalanceForEnergyV2(-delegateBalance);
        break;
      default:
        logger.debug("Resource Code Error.");
    }

    repo.updateAccount(ownerCapsule.createDbKey(), ownerCapsule);
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
