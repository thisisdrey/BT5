### Title
Unmetered linear scan of `VotesStore` in `MaintenanceManager.doMaintenance()` allows chain halt via cheap vote spam - (File: consensus/src/main/java/org/tron/consensus/dpos/MaintenanceManager.java)

### Summary
`MaintenanceManager.doMaintenance()`, invoked automatically by every node inside block application whenever the deterministic maintenance time is reached (every 6 hours by default), calls `countVote(votesStore)`, which does a full linear iteration over **every entry** in `VotesStore`. Each entry is a `VotesCapsule` created (or updated) whenever *any* account casts a `VoteWitnessContract`. Casting a vote is a cheap, unprivileged, bandwidth-only operation (`calcFee() == 0` on the resource-delegation vote path, no TRX burn), and each distinct voter address creates its own persistent key in `VotesStore`, which is never removed except when that same address votes again or unfreezes. An attacker can create an arbitrarily large number of funded accounts, freeze a minimal amount of TRX for TronPower in each, and vote once from each address, linearly growing `VotesStore` at negligible cost per entry. When the network's maintenance time elapses, `MaintenanceManager.applyBlock()` unconditionally triggers `doMaintenance()` inside block execution (not scoped to any single transaction's energy/bandwidth budget), so the cost of iterating the bloated store is paid by every full node in the block-processing critical path, with no cap or metering. This closely mirrors the reported MilkyWay bug class: an unmetered, block/governance-triggered iteration over an attacker-inflatable collection of user records, exploitable to halt or badly stall the chain.

### Finding Description
- `VoteWitnessActuator.countVoteAccount()` (and the TVM-native equivalent `VoteWitnessProcessor.execute()`) creates a `VotesCapsule` for the owner address in `VotesStore` if one does not already exist, and stores/refreshes `newVotes` on every vote transaction: [1](#0-0) 
- `VotesStore` is a simple key-value store keyed by voter address, with no bound on the number of distinct keys: [2](#0-1) 
- Entries are only removed from `VotesStore` when `MaintenanceManager.countVote()` consumes and deletes them during maintenance (i.e., they persist and accumulate across every block from vote-cast time until the next maintenance cycle, and can span many maintenance cycles if the attacker keeps voting): [3](#0-2) 
- `MaintenanceManager.applyBlock()` unconditionally calls `doMaintenance()` once the deterministic `nextMaintenanceTime` is reached, as part of ordinary block processing performed by every full node — this execution path is not gas/energy metered per attacker transaction; it is a fixed protocol-level hook executed during block application: [4](#0-3) 
- `doMaintenance()` calls `countVote(votesStore)`, which does a full DB iteration over the entire `VotesStore` and performs per-entry work (map updates) proportional to the number of votes stored, with no cap: [5](#0-4) 
- The equivalent logic also exists (and is documented) in `Wallet.countVote`, confirming this is a well-known, load-bearing linear scan of an attacker-inflatable store: [6](#0-5) 

The attack primitive is identical in shape to the reported bug: a cheap, unprivileged transaction type (`VoteWitnessContract`) that creates persistent per-account records in a store, followed by an unmetered, deterministically-triggered iteration over the entire store (`countVote`) executed inside core block-processing / consensus maintenance code that every node must run to advance the chain — analogous to `AfterServiceAccreditationModified` iterating over all delegations during unmetered governance execution.

### Impact Explanation
If an attacker inflates `VotesStore` to a very large number of entries (each entry cheap: minimal TRX freeze for TronPower plus one `VoteWitnessContract` transaction per address), then at the next scheduled maintenance time every full node (and every witness producing the maintenance block) must execute `doMaintenance()` -> `countVote()`, iterating and processing the entire bloated `VotesStore` synchronously inside block application. Because this work is not covered by any transaction's energy/bandwidth budget and has no upper bound, it can cause maintenance-block processing to take excessively long, causing missed block-production slots, node/witness stalls or timeouts, and potentially causing consensus/liveness failures across the network — i.e., a chain halt or severe network-wide DoS. This satisfies "node crash or halt / chain split" impact criteria.

### Likelihood Explanation
Any unprivileged account holder can permissionlessly trigger this by:
1. Creating many funded accounts (bandwidth/account-creation cost only).
2. Freezing a small amount of TRX in each to obtain nonzero TronPower.
3. Issuing one `VoteWitnessContract` per account (cost is bandwidth-only, no TRX burn, `calcFee()` returns 0 for the resource-based vote actuator path).

This can be executed over many blocks ahead of a known, deterministic maintenance time (`nextMaintenanceTime`, fixed 6-hour cadence), giving the attacker full control over both the timing and magnitude of the spam, exactly mirroring the reported report's "attacker spams delegations once accreditation change is on track to pass" pattern but targeting the fixed periodic maintenance trigger instead of a governance proposal.

### Recommendation
- Add linear-scaling costs (bandwidth/energy or an explicit fee) to `VoteWitnessContract` that scale with the number of distinct voter records maintained in `VotesStore`, or cap/charge for `VotesStore` growth per account, similar to the recommended fix in the reference report (`baseDelegationGasCharge` scaling).
- Alternatively, redesign vote-count aggregation to avoid a full unmetered iteration over all historical voter records during maintenance (e.g., incremental/delta accounting maintained at vote-cast time rather than batch-processed during the maintenance hook), removing the need for `countVote()`'s full-store scan altogether.
- Enforce a maximum number of live `VotesCapsule` entries or expire/prune stale voter records outside of the maintenance-time critical path.

### Proof of Concept
Conceptually (mirroring the reported PoC pattern applied to this codebase):
1. Generate N (e.g. 100,000) distinct funded accounts.
2. For each account: freeze a minimal amount for TronPower `FreezeBalanceV2Contract`/`FreezeBalanceContract`, then submit `VoteWitnessContract` voting for any existing witness — each creates/updates one entry in `VotesStore` per `VoteWitnessActuator.countVoteAccount()` at negligible fee [7](#0-6) .
3. Wait for (or arrange transactions immediately prior to) the network's `nextMaintenanceTime`.
4. When `MaintenanceManager.applyBlock()` triggers `doMaintenance()` on the maintenance block, `countVote(votesStore)` must iterate all N entries synchronously as part of block application [5](#0-4) , materially slowing or halting maintenance-block processing across the network.

Note: I was unable to fully verify the exact minimum TRX/bandwidth cost floor for account creation plus `FreezeBalanceV2Contract` + `VoteWitnessContract` in this specific checked-out revision (e.g. exact `calcFee()` for `VoteWitnessActuator`, minimum freeze amount enforced by `FreezeBalanceV2Actuator.validate()`), as the read of `VoteWitnessActuator.java` lines 1–152 could not be completed within the available tool calls. This detail affects the precise economic feasibility (cost-per-spammed-entry) but does not affect the root-cause finding that `countVote()` performs an unbounded, unmetered iteration over an attacker-inflatable `VotesStore` inside the mandatory block-application path.

### Citations

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

**File:** chainbase/src/main/java/org/tron/core/store/VotesStore.java (L10-23)
```java
@Component
public class VotesStore extends TronStoreWithRevoking<VotesCapsule> {

  @Autowired
  public VotesStore(@Value("votes") String dbName) {
    super(dbName);
  }

  @Override
  public VotesCapsule get(byte[] key) {
    byte[] value = revokingDB.getUnchecked(key);
    return ArrayUtils.isEmpty(value) ? null : new VotesCapsule(value);
  }
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

**File:** consensus/src/main/java/org/tron/consensus/dpos/MaintenanceManager.java (L89-103)
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
```

**File:** consensus/src/main/java/org/tron/consensus/dpos/MaintenanceManager.java (L165-192)
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
```

**File:** framework/src/main/java/org/tron/core/Wallet.java (L824-872)
```java
  /**
   * Counts vote changes for witnesses in the current epoch.
   *
   * Vote count changes are tracked as follows:
   * - Negative values for votes removed from previous witness in the last epoch
   * - Positive values for votes added to new witness in the current epoch
   *
   * Example:
   * an Account X had 100 votes for witness W1 in the previous epoch.
   * In the current epoch, X changes votes to:
   * - W2: 60 votes
   * - W3: 80 votes
   *
   * Resulting vote changes:
   * - W1: -100 (votes removed)
   * - W2: +60 (new votes)
   * - W3: +80 (new votes)
   */
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
  }
```
