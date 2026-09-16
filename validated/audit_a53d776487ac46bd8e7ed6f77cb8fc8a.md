### Title
Unbounded, unmetered iteration over `VotesStore` during DPoS maintenance enables permissionless chain halt - (File: `consensus/src/main/java/org/tron/consensus/dpos/MaintenanceManager.java`)

### Summary
`MaintenanceManager.doMaintenance()` is executed unconditionally during consensus block application (outside of transaction-level gas/energy metering) and performs a fully unbounded linear scan over **all** `VotesCapsule` entries currently stored in `VotesStore` via `countVote()`. Because casting a vote only requires freezing the chain minimum of 1 TRX, an attacker can permissionlessly flood `VotesStore` with an arbitrarily large number of entries across many cheap accounts/transactions before the next maintenance boundary, causing the maintenance-triggering block to take an unbounded amount of unmetered CPU/I/O time and halt block production.

### Finding Description
Every block, `Manager.processBlock()` calls `consensus.applyBlock(block)`, which for the DPoS consensus implementation calls `MaintenanceManager.applyBlock()`: [1](#0-0) 

`MaintenanceManager.applyBlock()` triggers `doMaintenance()` once the maintenance-time boundary is reached, entirely outside of any transaction/energy-metered execution path: [2](#0-1) 

Inside `doMaintenance()`, `countVote(votesStore)` iterates the **entire** `VotesStore` with no bound, size check, or gas accounting: [3](#0-2) 

`doMaintenance()` also performs additional unbounded, unmetered iterations over `getAllWitnesses()` for `accumulateWitnessVi` and `setBrokerage`/`setWitnessVote`, compounding the cost: [4](#0-3) [5](#0-4) 

The prerequisite for adding an entry to `VotesStore` is casting a vote, which requires only the chain-wide minimum freeze of 1 TRX: [6](#0-5) 

and vote count is only bounded per-transaction by `MAX_VOTE_NUMBER`, not globally, and voting is a fully permissionless, unprivileged operation available to any funded account: [7](#0-6) 

An attacker can therefore create a very large number of low-cost accounts (each funded with only slightly more than 1 TRX plus a small account-creation/bandwidth fee), have each freeze the minimum 1 TRX and cast a single vote, all within one maintenance epoch (default 6-hour cycle). At the maintenance boundary, `doMaintenance()`/`countVote()` will have to iterate over every one of these accumulated `VotesCapsule` entries — with DB reads, arithmetic, and a `votesStore.delete()` write per entry — with no gas metering or resource cost scaling with the number of entries, directly analogous to the reported unbounded `AllocateRewards` iteration over Rewards Plans in `BeginBlock`.

### Impact Explanation
If enough vote entries accumulate, the single block that crosses the maintenance-time boundary will take an unbounded amount of wall-clock time to process in `applyBlock()`, which happens in-line with block production/validation for every node in the network. This can stall or crash block production network-wide — a chain halt — analogous to the referenced `AllocateRewards` chain-halt bug. Because the cost per malicious "vote unit" is only ~1 TRX of *temporarily locked, not spent* balance (recoverable after unfreeze) plus negligible transaction fees, the attack is drastically cheaper than the 1 TIA-per-plan cost that was already deemed insufficient in the original report.

### Likelihood Explanation
- Voting and freezing are fully permissionless, requiring no special privilege beyond an account holding some TRX (obtainable via normal transfer, no admin/witness role needed).
- The per-entry cost floor is the network's absolute minimum freeze amount (1 TRX, refundable), making mass creation of `VotesStore` entries economically comparable to or cheaper than the reward-plan flood in the referenced report.
- The maintenance cycle is deterministic and public (`nextMaintenanceTime`), giving the attacker a known window to accumulate votes before the vulnerable unbounded loop executes.
- The vulnerable code (`doMaintenance`/`countVote`) executes unconditionally as part of consensus block application for every full node, not just an opt-in query path, so exploitation directly threatens network-wide liveness.

### Recommendation
- Cap the maximum number of distinct voters processed per maintenance cycle, or amortize/incrementally process `VotesStore` entries across multiple blocks rather than in a single unbounded pass.
- Introduce an economic cost that scales with the number of outstanding `VotesCapsule` entries (e.g., a global vote-slot fee or a minimum bandwidth/energy consumption charged at maintenance time proportional to store size), similar to the recommended scaling gas cost for the Rewards Plan fix.
- Add a hard upper bound on total active `VotesStore` size, rejecting new votes once the limit is reached, with the limit chosen so that worst-case iteration time in `doMaintenance()` remains bounded well within block-time budgets.

### Proof of Concept
1. Attacker creates `N` (e.g., hundreds of thousands) of fresh accounts, funding each with slightly more than `1 TRX` via ordinary transfers.
2. For each account, submit a `FreezeBalanceV2Contract` transaction freezing the minimum `1 TRX` for `BANDWIDTH` (validated in `FreezeBalanceV2Actuator.validate()`), then a `VoteWitnessContract` transaction casting `1` vote for any active witness.
3. Repeat step 2 for all `N` accounts across multiple blocks, all before `nextMaintenanceTime` is reached — each vote persists a `VotesCapsule` entry in `VotesStore` and is not cleared until the next `doMaintenance()` call.
4. Once the block whose timestamp crosses `nextMaintenanceTime` is produced, `MaintenanceManager.applyBlock()` invokes `doMaintenance()`, which calls `countVote(votesStore)` — this performs `N` DB reads plus `N` `votesStore.delete()` writes in a single unmetered pass, analogous to the original `TestAllocateRewards_RewardsPlanFlood` PoC, stalling block production for a duration that scales linearly with `N`.

### Citations

**File:** framework/src/main/java/org/tron/core/db/Manager.java (L1919-1927)
```java
    boolean flag = chainBaseManager.getDynamicPropertiesStore().getNextMaintenanceTime()
        <= block.getTimeStamp();
    if (flag) {
      proposalController.processProposals();
    }

    if (!consensus.applyBlock(block)) {
      throw new BadBlockException("consensus apply block failed");
    }
```

**File:** consensus/src/main/java/org/tron/consensus/dpos/MaintenanceManager.java (L57-82)
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
    //pbft block msg
    if (blockNum == 1) {
      nextMaintenanceTime = consensusDelegate.getNextMaintenanceTime();
    }
    pbftManager.blockPrePrepare(blockCapsule, nextMaintenanceTime);
  }
```

**File:** consensus/src/main/java/org/tron/consensus/dpos/MaintenanceManager.java (L96-101)
```java
    if (dynamicPropertiesStore.useNewRewardAlgorithm()) {
      long curCycle = dynamicPropertiesStore.getCurrentCycleNumber();
      consensusDelegate.getAllWitnesses().forEach(witness -> {
        delegationStore.accumulateWitnessVi(curCycle, witness.createDbKey(), witness.getVoteCount());
      });
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

**File:** consensus/src/main/java/org/tron/consensus/dpos/MaintenanceManager.java (L165-195)
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
  }
```

**File:** actuator/src/main/java/org/tron/core/actuator/FreezeBalanceV2Actuator.java (L131-137)
```java
    long frozenBalance = freezeBalanceV2Contract.getFrozenBalance();
    if (frozenBalance <= 0) {
      throw new ContractValidateException("frozenBalance must be positive");
    }
    if (frozenBalance < TRX_PRECISION) {
      throw new ContractValidateException("frozenBalance must be greater than or equal to 1 TRX");
    }
```

**File:** actuator/src/main/java/org/tron/core/vm/nativecontract/VoteWitnessProcessor.java (L28-36)
```java
  public void validate(VoteWitnessParam param, Repository repo) throws ContractValidateException {
    if (repo == null) {
      throw new ContractValidateException(STORE_NOT_EXIST);
    }

    if (param.getVotes().size() > MAX_VOTE_NUMBER) {
      throw new ContractValidateException(
          "VoteNumber more than maxVoteNumber " + MAX_VOTE_NUMBER);
    }
```
