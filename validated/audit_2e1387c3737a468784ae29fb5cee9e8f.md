This is exactly the bug class described in the report: a duplicate-entry check is missing in a list that is later processed additively/positionally, and the codebase shows the *fixed* pattern elsewhere (e.g., `AccountPermissionUpdateActuator.checkPermission()` explicitly rejects duplicate addresses via `.distinct()` comparison) but does **not** apply the same defense in the witness voting path.

### Title
Missing duplicate-vote-address check in `VoteWitnessActuator.validate()`/`countVoteAccount()` allows repeated witness entries in a single vote transaction - (File: `actuator/src/main/java/org/tron/core/actuator/VoteWitnessActuator.java`)

### Summary
`VoteWitnessActuator.validate()` iterates the `Vote` list in a `VoteWitnessContract` and validates each entry's address, witness existence, and vote count, and sums vote counts to compare against `tronPower` [1](#0-0) . Unlike `AccountPermissionUpdateActuator.checkPermission()`, which explicitly rejects duplicate key addresses using a `.distinct()` comparison [2](#0-1) , `VoteWitnessActuator.validate()` performs no such uniqueness check on `vote.getVoteAddress()` across the list. The only structural bound is `MAX_VOTE_NUMBER` (30) on `contract.getVotesCount()` [3](#0-2) .

### Finding Description
Repeated entries for the same witness address are accepted as long as the sum of all vote counts (including duplicates) does not exceed `tronPower`. This is confirmed by the existing test `vote1WitnssOneMoreTiems`, which builds a contract containing the *same* witness address repeated 30 times via `getRepeateContract` and successfully validates/executes it, with the witness's vote total simply incrementing by the sum [4](#0-3) . In `countVoteAccount()`, each vote entry is applied independently and additively via `votesCapsule.addNewVotes(...)` and `accountCapsule.addVotes(...)`, without any merging/dedup of same-address entries before storage [5](#0-4) . `VotesCapsule.addNewVotes()` simply appends a new `Vote` entry to the repeated `new_votes` field rather than merging by address [6](#0-5) , so the persisted `Votes` capsule (used later by `MaintenanceManager`/consensus for tallying witness vote counts) can end up containing multiple entries for the same witness address in a single account's vote list. This is analogous in nature to `SSVNetworkSSVNetwork.sol#registerValidator()`'s failure to reject duplicate operator IDs: a sorted/bounded-count check exists, but per-entry uniqueness is never enforced, and the consuming logic downstream (consensus tallying / vote-power accounting) is not designed to tolerate duplicate entries for the same key.

Notably, this contrasts with `VoteWitnessProcessor.execute()` — the TVM-native-contract equivalent — which *does* defend against this exact issue by merging votes into a `Map<ByteString, Long> voteMap` keyed by address before applying them [7](#0-6) . This asymmetry strongly suggests the merge-by-address behavior in `VoteWitnessProcessor` was added specifically because duplicate vote-address entries are a known hazard, while the older, transaction-broadcast-reachable `VoteWitnessActuator` path was never updated to apply the same defense.

### Impact Explanation
An unprivileged account broadcasting a single `VoteWitnessContract` transaction can list a witness address multiple times (up to `MAX_VOTE_NUMBER` = 30 entries). The result is that the account's persisted vote/votes state (`AccountCapsule.votes` and `VotesCapsule.new_votes`) contains multiple duplicate `Vote` entries for one witness, instead of the single merged entry that downstream witness-vote-counting/consensus logic expects. Depending on how later maintenance/consensus tallying and delta-computation (old vs new votes for witness ranking) processes a list with duplicate addresses rather than one entry, this can distort witness vote totals used to select active witnesses/committee members — a core consensus-integrity mechanism — without requiring any elevated privilege.

### Likelihood Explanation
High reachability: any account holder can freely construct a `VoteWitnessContract` with repeated `vote_address` entries and broadcast it through the standard transaction path; no special permission or witness/committee status is required, matching the "single signed transaction" reachability class.

### Recommendation
In `VoteWitnessActuator.validate()`, apply the same uniqueness enforcement used in `AccountPermissionUpdateActuator.checkPermission()` — reject the transaction if `vote.getVoteAddress()` values are not distinct across the `votes` list — or alternatively merge votes by address before persisting, mirroring the `voteMap` merge logic already present in `VoteWitnessProcessor.execute()`.

### Proof of Concept
1. Freeze balance for `OWNER_ADDRESS` to obtain sufficient `tronPower` (as in `voteWitness()` test setup) [8](#0-7) .
2. Build a `VoteWitnessContract` with N (≤30) duplicate `Vote` entries pointing at the same `WITNESS_ADDRESS`, using a helper equivalent to `getRepeateContract` [9](#0-8) .
3. Call `actuator.validate()` then `actuator.execute(ret)` — both succeed without any "duplicate vote address" error, as demonstrated by the existing `vote1WitnssOneMoreTiems` test [4](#0-3) .
4. Inspect `AccountCapsule.getVotesList()` / `VotesStore` entry for the account and confirm multiple duplicate `Vote` entries for the same witness address are persisted rather than a single merged entry.

**Uncertainty note:** I was not able to trace, within the available index, the exact downstream consensus/maintenance code path that consumes `VotesCapsule.new_votes`/`old_votes` to compute per-witness vote deltas, so I cannot conclusively prove a concrete double-counting or fund-impact outcome from these duplicate entries — only that the duplicate-uniqueness safeguard present elsewhere in the codebase is absent here, and that the sibling TVM implementation was specifically hardened against this scenario. A deeper investigation of `MaintenanceManager`/witness vote-count reconciliation logic (which the current index does not fully expose) would be needed to confirm exploitability with certainty; I recommend a full Devin session with repo access to trace this end-to-end if further validation is required.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/VoteWitnessActuator.java (L89-97)
```java
    if (contract.getVotesCount() == 0) {
      throw new ContractValidateException(
          "VoteNumber must more than 0");
    }
    int maxVoteNumber = MAX_VOTE_NUMBER;
    if (contract.getVotesCount() > maxVoteNumber) {
      throw new ContractValidateException(
          "VoteNumber more than maxVoteNumber " + maxVoteNumber);
    }
```

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

**File:** actuator/src/main/java/org/tron/core/actuator/VoteWitnessActuator.java (L178-191)
```java
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

**File:** actuator/src/main/java/org/tron/core/actuator/AccountPermissionUpdateActuator.java (L95-104)
```java
    long weightSum = 0;
    List<ByteString> addressList = permission.getKeysList()
        .stream()
        .map(x -> x.getAddress())
        .distinct()
        .collect(toList());
    if (addressList.size() != permission.getKeysList().size()) {
      throw new ContractValidateException(
          "address should be distinct in permission " + permission.getType());
    }
```

**File:** framework/src/test/java/org/tron/core/actuator/VoteWitnessActuatorTest.java (L121-130)
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
  }
```

**File:** framework/src/test/java/org/tron/core/actuator/VoteWitnessActuatorTest.java (L135-150)
```java
  @Test
  public void voteWitness() {
    long frozenBalance = 1_000_000_000_000L;
    long duration = 3;
    FreezeBalanceActuator freezeBalanceActuator = new FreezeBalanceActuator();
    freezeBalanceActuator.setChainBaseManager(dbManager.getChainBaseManager())
        .setAny(getContract(OWNER_ADDRESS, frozenBalance, duration));
    VoteWitnessActuator actuator = new VoteWitnessActuator();
    actuator.setChainBaseManager(dbManager.getChainBaseManager())
        .setAny(getContract(OWNER_ADDRESS, WITNESS_ADDRESS, 1L));
    TransactionResultCapsule ret = new TransactionResultCapsule();
    try {
      freezeBalanceActuator.validate();
      freezeBalanceActuator.execute(ret);
      actuator.validate();
      actuator.execute(ret);
```

**File:** framework/src/test/java/org/tron/core/actuator/VoteWitnessActuatorTest.java (L402-427)
```java
  @Test
  public void vote1WitnssOneMoreTiems() {
    long frozenBalance = 1_000_000_000_000L;
    long duration = 3;
    FreezeBalanceActuator freezeBalanceActuator = new FreezeBalanceActuator();
    freezeBalanceActuator.setChainBaseManager(dbManager.getChainBaseManager())
        .setAny(getContract(OWNER_ADDRESS, frozenBalance, duration));
    VoteWitnessActuator actuator = new VoteWitnessActuator();
    actuator.setChainBaseManager(dbManager.getChainBaseManager())
        .setAny(getRepeateContract(OWNER_ADDRESS, WITNESS_ADDRESS, 1L, 30));
    TransactionResultCapsule ret = new TransactionResultCapsule();
    try {
      freezeBalanceActuator.validate();
      freezeBalanceActuator.execute(ret);
      actuator.validate();
      actuator.execute(ret);

      maintenanceManager.doMaintenance();
      WitnessCapsule witnessCapsule = dbManager.getWitnessStore()
          .get(StringUtil.hexString2ByteString(WITNESS_ADDRESS).toByteArray());
      Assert.assertEquals(10 + 30, witnessCapsule.getVoteCount());
    } catch (ContractValidateException e) {
      Assert.assertFalse(e instanceof ContractValidateException);
    } catch (ContractExeException e) {
      Assert.assertFalse(e instanceof ContractExeException);
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
