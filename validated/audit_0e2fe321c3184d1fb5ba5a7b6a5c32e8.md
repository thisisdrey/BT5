### Title
Unbounded growth of `VotesStore` allows cheap Sybil vote-griefing that inflates per-block maintenance cost for every full node/SR - ([File: consensus/src/main/java/org/tron/consensus/dpos/MaintenanceManager.java])

### Summary
`MaintenanceManager.doMaintenance()`, which runs inside `applyBlock()` on every maintenance boundary (a normal, network-wide block-application path reachable purely by broadcasting `VoteWitnessContract`/`FreezeBalanceV2Contract` transactions), calls `countVote(votesStore)`, which does a full linear scan over **every entry in `VotesStore`** [1](#0-0) . `VotesStore` gets one entry per distinct account address that has ever cast a vote (via `VoteWitnessActuator.countVoteAccount` / `VoteWitnessProcessor.execute`), and there is no upper bound on the number of distinct voter accounts [2](#0-1) [3](#0-2) . Casting a vote only requires an account to have non-zero TRON Power (obtainable with a minimal freeze, e.g. 1 TRX), so an attacker can cheaply create a large number of accounts, freeze the minimum amount, and cast 1-vote transactions for a witness. Each such transaction adds a distinct key to `VotesStore`; because keys are deleted only when `countVote` runs (once per maintenance cycle), an attacker can keep the store artificially large indefinitely by re-voting each cycle at negligible cost, while its size scales with the total number of distinct voter accounts network-wide, not per-account state.

### Finding Description
This is the same bug class as the reported `netAtPrice/depositAuction/withdrawAuction` griefing: a per-user-controlled, unbounded array/collection is fully iterated by privileged/maintenance logic, and the attacker's cost (a few sun for a minimal freeze + vote) is far lower than the aggregate cost imposed on the victims (every witness/full node that must apply the block).

Concretely:
1. `VoteWitnessActuator.countVoteAccount` (actuator path) and `VoteWitnessProcessor.execute` (TVM native-contract path) both write a `VotesCapsule` keyed by the voter's address into `VotesStore` for any account with `getTronPower() > 0`, with no minimum vote-count or account-count restriction beyond `MAX_VOTE_NUMBER` witnesses per vote (30) [4](#0-3) .
2. On every maintenance boundary, `MaintenanceManager.applyBlock` → `doMaintenance` → `countVote(votesStore)` opens a DB iterator and walks **the entire `VotesStore`**, processing `getOldVotes()`/`getNewVotes()` for each entry and then deleting it [5](#0-4) [6](#0-5) .
3. The identical unbounded scan is also reachable synchronously from the read-query path `Wallet.getPaginatedNowWitnessList`, which recomputes `countVote(votesStore)` on every call during a maintenance-in-progress window, meaning a public API (`getPaginatedNowWitnessList` gRPC/HTTP endpoint) also pays the full O(N) cost for every request [7](#0-6) [8](#0-7) .

Since there is no cap on the number of distinct addresses that can appear as keys in `VotesStore`, and each key requires only a trivial resource freeze to create, this is directly analogous to the reported `deposits`/`withdraws` unbounded-array griefing: the cost of the malicious action (many cheap 1-wei-equivalent votes) is externalized onto the party that must process the aggregate collection (here, every node applying a block, and every caller of the witness-list API), rather than onto the attacker.

### Impact Explanation
Every full node and every witness (SR) must execute `doMaintenance()` at every maintenance interval; an attacker-inflated `VotesStore` directly increases the CPU/DB time consumed by *all* nodes on the network at that fixed point in time, which can degrade block production latency or, at sufficient scale, contribute to missed block/maintenance-processing deadlines network-wide (rather than harming a single "owner" as in the original report, this is broader since it affects block application in `Manager`/`MaintenanceManager`, an allowed target). It also affects the public `getPaginatedNowWitnessList` query, forcing every caller (any anonymous API client) to pay the same O(N) recomputation cost each time it's invoked during maintenance windows, degrading the node's ability to serve that API under load.

### Likelihood Explanation
The attack requires only the ability to submit ordinary `FreezeBalanceV2Contract`/`VoteWitnessContract` transactions (or the TVM `voteWitness` native contract) with the network's minimum freeze amount, which any unprivileged transaction broadcaster can do. The main cost to the attacker is transaction bandwidth/fees for creating and funding many accounts and repeatedly voting each maintenance cycle — cheap relative to the linear cost imposed on every node in the network. No special privileges, races, or unusual preconditions are required.

### Recommendation
- Bound the growth of `VotesStore` per maintenance cycle, e.g. by requiring a minimum stake/vote-count threshold before a `VotesCapsule` entry is retained, or by capping the number of processed entries per maintenance run and carrying over the remainder.
- Avoid recomputing the full `countVote` scan synchronously inside a hot read path (`Wallet.getPaginatedNowWitnessList`); cache/memoize the result for the duration of the maintenance window instead of recomputing per request.
- Consider aggregating votes by witness incrementally at vote-cast time (as `WitnessCapsule.voteCount` deltas) rather than deferring all aggregation to a single full-store scan at maintenance time, removing the O(N) dependency on distinct voter count entirely.

### Proof of Concept
1. Attacker creates `N` new accounts and activates each with a minimal TRX transfer.
2. For each account, submit `FreezeBalanceV2Contract` to freeze the minimum amount (e.g. 1 TRX) for `BANDWIDTH`/`TRON_POWER`, granting `tronPower = 1`.
3. Submit `VoteWitnessContract` (or invoke `voteWitness` via the TVM native contract) from each account, casting `1` vote for an arbitrary witness. Each transaction is accepted by `VoteWitnessProcessor.validate`/`execute` because it only checks `votes.size() <= MAX_VOTE_NUMBER` and `sum <= tronPower` [9](#0-8) .
4. Repeat for a large `N` (e.g. tens of thousands of accounts) before the next maintenance boundary.
5. At maintenance time, every full node/SR calls `doMaintenance()` → `countVote(votesStore)`, iterating all `N` newly created entries in `VotesStore`, and any concurrent `getPaginatedNowWitnessList` API caller triggers the same O(N) scan again [10](#0-9) .
6. Repeating steps 2–4 every maintenance cycle keeps the attack cost low while the aggregate victim-side cost (summed over the whole network of nodes/SRs) remains high, mirroring the original report's griefing pattern.

### Citations

**File:** consensus/src/main/java/org/tron/consensus/dpos/MaintenanceManager.java (L57-76)
```java
  public void applyBlock(BlockCapsule blockCapsule) {
    long blockNum = blockCapsule.getNum();
    long blockTime = blockCapsule.getTimeStamp();
    long nextMaintenanceTime = consensusDelegate.getNextMaintenanceTime();
    boolean flag = consensusDelegate.getNextMaintenanceTime() <= blockTime;
    if (flag) {
      if (blockNum != 1) {
        updateWitnessValue(beforeWitness);
        beforeMaintenanceTime = nextMaintenanceTime;
        doMaintenance();
        updateWitnessValue(currentWitness);
      }
      consensusDelegate.updateNextMaintenanceTime(blockTime);
      if (blockNum != 1) {
        //pbft sr msg
        pbftManager.srPrePrepare(blockCapsule, currentWitness,
            consensusDelegate.getNextMaintenanceTime());
      }
    }
    consensusDelegate.saveStateFlag(flag ? 1 : 0);
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

**File:** consensus/src/main/java/org/tron/consensus/dpos/MaintenanceManager.java (L165-194)
```java
  private Map<ByteString, Long> countVote(VotesStore votesStore) {
    final Map<ByteString, Long> countWitness = Maps.newHashMap();
    Iterator<Entry<byte[], VotesCapsule>> dbIterator = votesStore.iterator();
    long sizeCount = 0;
    while (dbIterator.hasNext()) {
      Entry<byte[], VotesCapsule> next = dbIterator.next();
      VotesCapsule votes = next.getValue();
      votes.getOldVotes().forEach(vote -> {
        ByteString voteAddress = vote.getVoteAddress();
        long voteCount = vote.getVoteCount();
        if (countWitness.containsKey(voteAddress)) {
          countWitness.put(voteAddress, countWitness.get(voteAddress) - voteCount);
        } else {
          countWitness.put(voteAddress, -voteCount);
        }
      });
      votes.getNewVotes().forEach(vote -> {
        ByteString voteAddress = vote.getVoteAddress();
        long voteCount = vote.getVoteCount();
        if (countWitness.containsKey(voteAddress)) {
          countWitness.put(voteAddress, countWitness.get(voteAddress) + voteCount);
        } else {
          countWitness.put(voteAddress, voteCount);
        }
      });
      sizeCount++;
      votesStore.delete(next.getKey());
    }
    logger.info("There is {} new votes in this epoch", sizeCount);
    return countWitness;
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

**File:** actuator/src/main/java/org/tron/core/vm/nativecontract/VoteWitnessProcessor.java (L28-111)
```java
  public void validate(VoteWitnessParam param, Repository repo) throws ContractValidateException {
    if (repo == null) {
      throw new ContractValidateException(STORE_NOT_EXIST);
    }

    if (param.getVotes().size() > MAX_VOTE_NUMBER) {
      throw new ContractValidateException(
          "VoteNumber more than maxVoteNumber " + MAX_VOTE_NUMBER);
    }
  }

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
    } catch (ArithmeticException e) {
      throw new ContractExeException(e.getMessage());
    }

    for (Map.Entry<ByteString, Long> entry : voteMap.entrySet()) {
      accountCapsule.addVotes(entry.getKey(), entry.getValue());
      votesCapsule.addNewVotes(entry.getKey(), entry.getValue());
    }
    repo.updateAccount(accountCapsule.createDbKey(), accountCapsule);
    repo.updateVotes(ownerAddress, votesCapsule);
  }
```

**File:** framework/src/main/java/org/tron/core/Wallet.java (L792-822)
```java
    // It contains the final vote count at the end of the last epoch.
    List<WitnessCapsule> witnessCapsuleList = chainBaseManager.getWitnessStore().getAllWitnesses();
    if (offset >= witnessCapsuleList.size()) {
      return null;
    }

    VotesStore votesStore = chainBaseManager.getVotesStore();
    // Count the vote changes for each witness in the current epoch, it is maybe negative.
    Map<ByteString, Long> countWitness = countVote(votesStore);

    // Iterate through the witness list to apply vote changes and calculate the real-time vote count
    witnessCapsuleList.forEach(witnessCapsule -> {
      long voteCount = countWitness.getOrDefault(witnessCapsule.getAddress(), 0L);
      witnessCapsule.setVoteCount(witnessCapsule.getVoteCount() + voteCount);
    });

    // Use the same sorting logic as in the Maintenance period
    WitnessStore.sortWitnesses(witnessCapsuleList,
        chainBaseManager.getDynamicPropertiesStore().allowWitnessSortOptimization());

    List<WitnessCapsule> sortedWitnessList = witnessCapsuleList.stream()
        .skip(offset)
        .limit(limit)
        .collect(Collectors.toList());

    WitnessList.Builder builder = WitnessList.newBuilder();
    sortedWitnessList.forEach(witnessCapsule ->
        builder.addWitnesses(witnessCapsule.getInstance()));

    return builder.build();
  }
```

**File:** framework/src/main/java/org/tron/core/Wallet.java (L842-871)
```java
  private Map<ByteString, Long> countVote(VotesStore votesStore) {
    // Initialize a result map to store vote changes for each witness
    Map<ByteString, Long> countWitness = Maps.newHashMap();

    // VotesStore is a key-value store, where the key is the address of the voter
    Iterator<Entry<byte[], VotesCapsule>> dbIterator = votesStore.iterator();

    while (dbIterator.hasNext()) {
      Entry<byte[], VotesCapsule> next = dbIterator.next();
      VotesCapsule votes = next.getValue();

      /**
       * VotesCapsule contains two lists:
       * - Old votes: Last votes from the previous epoch, updated in maintenance period
       * - New votes: Latest votes in current epoch, updated after each vote transaction
       */
      votes.getOldVotes().forEach(vote -> {
        ByteString voteAddress = vote.getVoteAddress();
        long voteCount = vote.getVoteCount();
        countWitness.put(voteAddress,
            countWitness.getOrDefault(voteAddress, 0L) - voteCount);
      });
      votes.getNewVotes().forEach(vote -> {
        ByteString voteAddress = vote.getVoteAddress();
        long voteCount = vote.getVoteCount();
        countWitness.put(voteAddress,
            countWitness.getOrDefault(voteAddress, 0L) + voteCount);
      });
    }
    return countWitness;
```
