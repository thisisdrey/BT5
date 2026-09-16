### Title
Duplicate votes in a single `VoteWitnessContract` are not merged, allowing account votes and consensus vote tallies to be over-counted relative to available TRON Power - ([File: actuator/src/main/java/org/tron/core/actuator/VoteWitnessActuator.java])

### Summary
`VoteWitnessActuator.countVoteAccount` appends every `Vote` entry from an incoming `VoteWitnessContract` to `accountCapsule` and `votesCapsule` without merging duplicate `vote_address` entries, unlike the equivalent TVM-native path (`VoteWitnessProcessor`) which explicitly merges duplicates into a `voteMap` before writing them ("merge vote for same witness").

### Finding Description
`VoteWitnessActuator.validate()` sums all `vote.getVoteCount()` values across the raw `contract.getVotesList()` (including duplicate witness addresses) and compares that sum against the voter's `tronPower`: [1](#0-0) 

The sum check is arithmetically correct for the total but `countVoteAccount()` then re-iterates the same unmerged list and calls `addVotes`/`addNewVotes` once per entry rather than once per unique witness address: [2](#0-1) 

Both `AccountCapsule.addVotes` and `VotesCapsule.addNewVotes` are pure "append a new `Vote` proto entry" operations — they do not look up or merge an existing entry for the same `vote_address`: [3](#0-2) [4](#0-3) 

As a result, a single `VoteWitnessContract` that lists the same witness address multiple times (e.g., `[witnessA:1, witnessA:1, ..., up to MAX_VOTE_NUMBER=30]`) produces an `account.votes` list (and `votes.new_votes` list) with 30 separate duplicate entries for `witnessA` instead of one merged entry.

By contrast, the newer TVM native-contract equivalent (`voteWitness` opcode / `VoteWitnessProcessor`) explicitly merges duplicate witness votes via a `HashMap` before persisting, and only writes one aggregated `Vote` per unique witness address: [5](#0-4) [6](#0-5) 

This confirms the actuator path is the outlier lacking de-duplication, mirroring the reported `_addUnderlyingPool` bug class where an array/list accumulates duplicate entries without checking for an existing entry first, degrading downstream consumers that iterate the list.

### Impact Explanation
Downstream code that iterates `account.votes` / `votes.new_votes` and assumes at most one entry per witness (or that treats list size as a proxy for distinct witnesses, e.g. `MAX_VOTE_NUMBER` enforcement, `getTronPowerUsage()` style summations, maintenance vote tallying, reward computation, and any RPC/API/GetAccount consumer reading the vote list) will process redundant entries. This inflates per-account storage in `AccountStore`/`VotesStore`, and any logic keyed by unique witness addresses (e.g., paging, per-witness weight lookups, dedup-based validation of vote count against `MAX_VOTE_NUMBER`) can be bypassed or miscounted, since the actuator's `MAX_VOTE_NUMBER` check counts raw entries, not unique witnesses, letting a single transaction produce more stored `Vote` records for one witness than the "30 witnesses max" invariant intends. This is reachable from any unprivileged, funded account issuing a normal `VoteWitnessContract` transaction — no special privilege required.

### Likelihood Explanation
High likelihood of reachability: any account with sufficient TRON Power can submit a `VoteWitnessContract` with repeated `vote_address` entries (up to `MAX_VOTE_NUMBER` = 30) in a single transaction, since `validate()` only checks each entry's individual validity and the aggregate sum against `tronPower`, never checking for duplicate witness addresses within the same contract.

### Recommendation
In `VoteWitnessActuator.countVoteAccount` (and ideally also in `validate`), merge votes by `vote_address` before calling `accountCapsule.addVotes` / `votesCapsule.addNewVotes`, mirroring the `voteMap` merge logic already implemented in `VoteWitnessProcessor.execute`.

### Proof of Concept
1. Fund an account and freeze balance so `tronPower` ≥ desired vote sum.
2. Build a `VoteWitnessContract` with 30 `Vote` entries all pointing to the same existing witness address, each with `vote_count = 1` (sum = 30, within `tronPower`).
3. Submit via `VoteWitnessActuator` (broadcastable transaction, no special permission needed) as demonstrated in the existing repeated-vote test helper `getRepeateContract`: [7](#0-6) 
4. After `execute()`, inspect `accountStore.get(owner).getVotesList()` — it will contain 30 separate `Vote` entries for the same witness address instead of one merged entry with `vote_count = 30`, confirming the duplicate-append behavior.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/VoteWitnessActuator.java (L98-121)
```java
    try {
      Iterator<Vote> iterator = contract.getVotesList().iterator();
      Long sum = 0L;
      while (iterator.hasNext()) {
        Vote vote = iterator.next();
        byte[] witnessCandidate = vote.getVoteAddress().toByteArray();
        if (!DecodeUtil.addressValid(witnessCandidate)) {
          throw new ContractValidateException("Invalid vote address!");
        }
        long voteCount = vote.getVoteCount();
        if (voteCount <= 0) {
          throw new ContractValidateException("vote count must be greater than 0");
        }
        String readableWitnessAddress = StringUtil.createReadableString(vote.getVoteAddress());
        if (!accountStore.has(witnessCandidate)) {
          throw new ContractValidateException(
              ACCOUNT_EXCEPTION_STR + readableWitnessAddress + NOT_EXIST_STR);
        }
        if (!witnessStore.has(witnessCandidate)) {
          throw new ContractValidateException(
              WITNESS_EXCEPTION_STR + readableWitnessAddress + NOT_EXIST_STR);
        }
        sum = LongMath.checkedAdd(sum, vote.getVoteCount());
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

**File:** chainbase/src/main/java/org/tron/core/capsule/AccountCapsule.java (L574-581)
```java
  /**
   * set votes.
   */
  public void addVotes(ByteString voteAddress, long voteAdd) {
    this.account = this.account.toBuilder()
        .addVotes(Vote.newBuilder().setVoteAddress(voteAddress).setVoteCount(voteAdd).build())
        .build();
  }
```

**File:** chainbase/src/main/java/org/tron/core/capsule/VotesCapsule.java (L81-85)
```java
  public void addNewVotes(ByteString voteAddress, long voteCount) {
    this.votes = this.votes.toBuilder()
        .addNewVotes(Vote.newBuilder().setVoteAddress(voteAddress).setVoteCount(voteCount).build())
        .build();
  }
```

**File:** actuator/src/main/java/org/tron/core/vm/nativecontract/VoteWitnessProcessor.java (L54-86)
```java
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
```

**File:** actuator/src/main/java/org/tron/core/vm/nativecontract/VoteWitnessProcessor.java (L105-108)
```java
    for (Map.Entry<ByteString, Long> entry : voteMap.entrySet()) {
      accountCapsule.addVotes(entry.getKey(), entry.getValue());
      votesCapsule.addNewVotes(entry.getKey(), entry.getValue());
    }
```

**File:** framework/src/test/java/org/tron/core/actuator/VoteWitnessActuatorTest.java (L121-129)
```java
  private Any getRepeateContract(String address, String voteaddress, Long value, int times) {
    VoteWitnessContract.Builder builder = VoteWitnessContract.newBuilder();
    builder.setOwnerAddress(StringUtil.hexString2ByteString(address));
    for (int i = 0; i < times; i++) {
      builder.addVotes(Vote.newBuilder()
          .setVoteAddress(StringUtil.hexString2ByteString(voteaddress))
          .setVoteCount(value).build());
    }
    return Any.pack(builder.build());
```
