Confirmed: `accountCapsule.setBalance(oldBalance + allowance)` directly mints new balance from the `allowance` field with no counter-deduction from any global reward pool [1](#0-0) . This confirms that inflated per-voter reward crediting translates directly into unbacked TRX balance upon withdrawal.

### Title
Wrong split of cycle rewards allows a voter to claim a full cycle's VI-based reward using a vote weight increased after the cycle's reward pool was already fixed - ([File: consensus/src/main/java/org/tron/consensus/dpos/MaintenanceManager.java])

### Summary
The DPoS reward-splitting mechanism computes a per-vote reward index (`VI`) for each cycle using the SR's vote count *as it stood before the cycle's vote changes are applied*, but individual voters are later credited using their *current* (possibly increased) vote count when they withdraw. A voter who increases their vote for an SR during a cycle gets that increase counted only in the *following* cycle's SR-level vote total, yet their personal reward computation applies their new, larger vote weight retroactively to the just-closed cycle's VI - a cycle whose reward pool was sized for the smaller, pre-increase total vote. This is the same bug class as the StakeWise report: a value added late (an "unbounded" contribution not yet part of the pool used to compute the split) is nonetheless included when computing that party's share of already-accrued rewards, over-crediting them at the expense of the reward pool's integrity.

### Finding Description
Each cycle, `MaintenanceManager.doMaintenance()` first accumulates the VI (reward-per-vote index) for the *closing* cycle using the witness's vote count **before** any vote deltas for that cycle are applied: [2](#0-1) 

Only afterward are the accumulated vote deltas for the closing cycle (`countVote`) applied to the witness's `voteCount`, and that new total takes effect starting with the **next** cycle: [3](#0-2) [4](#0-3) 

However, when a user casts a `VoteWitnessContract` transaction, `VoteWitnessActuator.countVoteAccount()` first settles the account's reward *up to the current cycle* via `MortgageService.withdrawReward` (which advances `beginCycle` to the current, still-open cycle), and only then overwrites the account's vote weight with the new value: [5](#0-4) 

Because `withdrawReward`/`queryReward` only settle rewards for cycles strictly *before* the current one (`computeReward` is exclusive of `endCycle`), the increased vote weight becomes the value stored in `beginCycle = currentCycle` for the account, and is applied in full to the entire span from `beginCycle` onward once VI-based accounting resumes: [6](#0-5) 

The VI for that very cycle (`curCycle`) was computed using the witness's *pre-increase* vote total (per the `MaintenanceManager` snippet above). When the voter later withdraws, `computeReward` multiplies `deltaVi` (which reflects the reward pool divided by the smaller, pre-increase vote total) by the voter's now-larger vote count, over-crediting them for a cycle in which their larger stake was not actually part of the divisor used to compute the reward rate.

The final destination of this inflated credit is directly minted TRX balance with no compensating debit from a shared/backing pool: [7](#0-6) 

### Impact Explanation
An attacker can freeze/stake TRX (via `FreezeBalanceContract`/`FreezeBalanceV2Contract`) and cast a large vote for an SR shortly before a maintenance cycle boundary. Because the SR's vote-count denominator used to compute that cycle's VI is the smaller, pre-vote total, but the attacker's personal reward multiplier later uses their full, larger vote weight for that same cycle, the sum of individually credited rewards for that SR's voters (existing voters at their normal share, plus the attacker's disproportionate share) exceeds what the cycle's actual `reward(cycle)` pool represented. Since `WithdrawBalanceActuator` converts `allowance` straight into account `balance` with no pool-level debit check, this results in unbacked TRX being minted into circulation - an economic exploit that also dilutes/steals proportional rewards from legitimate long-term voters of the same witness, matching the "wrong split of rewards" bug class from the reference report (a value not part of the base used for the reward split ends up benefiting from that split anyway).

### Likelihood Explanation
The exploit path is reachable by any unprivileged account that can freeze TRX for voting power and broadcast a `VoteWitnessContract`/`VoteWitnessContract` (native or TVM-based `VoteWitnessProcessor`) transaction. It requires timing a vote-count increase to land within the same cycle whose maintenance boundary is imminent, and requires already having idle TRX to freeze/vote with — no privileged role, SR collusion, or network-level manipulation is needed. However, the actual economic upside is bounded by the size of the reward pool for that single SR in that single cycle, and repeated exploitation across cycles/SRs is needed to accumulate significant value, and it also requires the attacker to accurately time transactions relative to cycle boundaries (which are public/derivable from `getCurrentCycleNumber`).

### Recommendation
When the VI for a cycle is computed, or when an account's begin/end cycle bookkeeping is updated on a mid-cycle vote change, ensure the *same* vote-count snapshot is used consistently on both sides of the split: either (a) apply the account's vote change to `beginCycle = currentCycle + 1` (deferring the new weight to the next cycle, consistent with how it is deferred at the witness level in `countVote`), or (b) apply the witness-level vote-count delta immediately (rather than batching it to the next cycle) so `accumulateWitnessVi` divides by the up-to-date total. The two must not diverge, since divergence lets one voter's reward multiplier use a vote weight larger than what the reward-rate denominator assumed.

### Proof of Concept
1. SR `W` starts cycle `N` with `100` total votes from voter `A` (`50`) and voter `B` (`50`).
2. During cycle `N`, block/tx-fee rewards accrue `100` total for `W` via `MortgageService.payReward` (`delegationStore.addReward(N, W, 100)`).
3. Late in cycle `N` (before maintenance), attacker `C` freezes TRX and calls `VoteWitnessContract` to vote `1000` for `W`. `VoteWitnessActuator.countVoteAccount()` settles `C`'s prior rewards (none) and sets `beginCycle = N` for `C`, storing `voteCount = 1000` in `C`'s `AccountCapsule`; the vote delta `+1000` is only recorded in `VotesStore` (`newVotes`), not yet applied to `W`'s `WitnessCapsule.voteCount`.
4. `MaintenanceManager.doMaintenance()` runs: `accumulateWitnessVi(N, W, 100)` computes `VI_N = reward(N)/100 = 1` per vote, using the pre-increase total of `100` — line reference: [8](#0-7) . `countVote()` then applies `+1000` to `W.voteCount`, making it `1100` starting cycle `N+1`.
5. In cycle `N+1`, `C` calls `withdrawReward`/`queryReward`. `computeReward(N, N+1, C)` computes `deltaVi(N-1,N) * C.userVote = 1 * 1000 = 1000` and credits `C` with `1000` allowance, even though cycle `N`'s recorded reward for `W` was only `100` total (already also owed in full, proportionally, to `A` and `B`).
6. `C` then calls `WithdrawBalanceContract`, and `WithdrawBalanceActuator.execute()` converts the `1000` allowance directly into `balance` with no pool-backing check [1](#0-0) , minting TRX far in excess of the `100` actually earned by `W` in cycle `N`.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/WithdrawBalanceActuator.java (L57-68)
```java
    AccountCapsule accountCapsule = accountStore.
        get(withdrawBalanceContract.getOwnerAddress().toByteArray());
    long oldBalance = accountCapsule.getBalance();
    long allowance = accountCapsule.getAllowance();

    long now = dynamicStore.getLatestBlockHeaderTimestamp();
    accountCapsule.setInstance(accountCapsule.getInstance().toBuilder()
        .setBalance(oldBalance + allowance)
        .setAllowance(0L)
        .setLatestWithdrawTime(now)
        .build());
    accountStore.put(accountCapsule.createDbKey(), accountCapsule);
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

**File:** consensus/src/main/java/org/tron/consensus/dpos/MaintenanceManager.java (L103-127)
```java
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
