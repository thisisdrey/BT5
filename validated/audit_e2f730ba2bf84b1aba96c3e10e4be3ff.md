### Title
Unbounded VotesStore Growth Causes O(n) Iteration DoS in Maintenance Block Application - (File: consensus/src/main/java/org/tron/consensus/dpos/MaintenanceManager.java)

### Summary
`MaintenanceManager.countVote()` iterates over every entry in `VotesStore` on every maintenance cycle (executed by every full node/SR as part of block application). Each entry is created by an ordinary, unprivileged `VoteWitnessContract` transaction, and there is no cap on the number of distinct voter entries that can accumulate in a single epoch. An attacker can create many low-cost accounts, freeze the minimum amount required to obtain Tron Power, and cast a vote from each — inflating `VotesStore` to an arbitrarily large size before the next maintenance tick. This mirrors the SHToken `users` array DoS pattern: an unbounded, attacker-growable collection that a core, universally-executed function must fully iterate.

### Finding Description
`VoteWitnessActuator.countVoteAccount()` writes one `VotesCapsule` entry into `VotesStore` per voting account address, keyed by owner address: [1](#0-0) 

This path is reachable by any unprivileged signer who has any Tron Power (obtained via `FreezeBalanceContract`/`FreezeBalanceV2Contract`, requiring only a minimal frozen amount), so an attacker can mass-produce entries by creating many accounts and voting from each — a cheap, permissionless operation analogous to "transferring 1 unit to 14,600 addresses" in the SHToken report.

At every maintenance boundary, `MaintenanceManager.doMaintenance()` — part of block application executed by every node — calls `countVote(votesStore)`, which opens a full iterator over the entire `VotesStore` and processes every entry, then deletes each one: [2](#0-1) [3](#0-2) 

There is no bound on the number of distinct addresses that can vote within a single epoch before the store is cleared, so the size of the iteration set is entirely attacker-controlled, exactly like the unbounded `users` array in SHToken's `deleteUserFromArray()`.

A second, equivalent unbounded-iteration path exists in `Wallet.getPaginatedNowWitnessList()`, which also fully drains `VotesStore` via its own `countVote()` on every query call: [4](#0-3) 

This means the same unbounded collection can also be scanned on-demand through the wallet/gRPC query path, multiplying the DoS surface (query API into Wallet is explicitly in scope).

### Impact Explanation
Because `doMaintenance()` runs inside block application on every node (SR and non-SR) at every maintenance cycle, an attacker who inflates `VotesStore` to a very large size can force every node in the network to perform a proportionally large, unbounded iteration/processing pass during block application. If the number of entries is large enough, this increases block-processing latency network-wide, risking missed block slots, degraded consensus timing, or node stalls under sufficiently large vote-spam — a chain-wide availability impact rather than an isolated account failure, consistent with the "node crash or halt" / "chain split" impact classes permitted by the rules.

### Likelihood Explanation
Any account holder can trigger this without special privilege: only account creation (cheap) and the minimum freeze amount required to obtain nonzero Tron Power are needed, then a single `VoteWitnessContract` transaction per account. This is directly analogous to the SHToken PoC's low per-unit cost multiplied across many addresses, and requires no validator/SR/committee privilege, no p2p manipulation, and no code deployment — it is reachable purely through standard signed transactions.

### Recommendation
Bound the number of unique voter entries processed per maintenance cycle (e.g., cap active voters, require higher minimum stake per vote, or paginate/checkpoint the `VotesStore` scan across multiple blocks instead of doing it atomically in one maintenance pass). Consider indexing votes by witness rather than by voter to avoid a full-store scan, or enforce economic disincentives (e.g., minimum frozen balance thresholds) that make large-scale unique-voter spam prohibitively expensive relative to its DoS impact.

### Proof of Concept
Conceptual reproduction (analogous to the SHToken PoC):
1. Programmatically create N (e.g., tens of thousands) of new accounts, funding each with the minimum TRX needed for activation and freezing.
2. From each account, freeze the minimum allowed balance via `FreezeBalanceV2Contract` to obtain nonzero Tron Power.
3. From each account, submit a `VoteWitnessContract` transaction voting for any witness, causing `VoteWitnessActuator.countVoteAccount()` to insert one `VotesCapsule` entry per account into `VotesStore`.
4. Before the next maintenance boundary, `VotesStore` contains N attacker-controlled entries.
5. When `MaintenanceManager.doMaintenance()` runs (or when any client calls `Wallet.getPaginatedNowWitnessList()`), `countVote()` must fully iterate all N entries, measurably increasing block-application time / query latency proportional to N, with no upper bound enforced by the protocol. [5](#0-4) [3](#0-2)

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

**File:** consensus/src/main/java/org/tron/consensus/dpos/MaintenanceManager.java (L89-104)
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
