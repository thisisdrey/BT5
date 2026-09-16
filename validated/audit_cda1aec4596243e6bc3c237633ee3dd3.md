### Title
No incentive/penalty for timing of witness votes allows last-block whale manipulation of SR elections - (File: consensus/src/main/java/org/tron/consensus/dpos/MaintenanceManager.java)

### Summary
Hermez's bidding/voting system counts votes with full, un-decayed weight regardless of when they are cast, letting a large-fund actor bid/vote at the very last moment and decide the outcome without giving earlier participants a chance to react. The same pattern exists in java-tron's Super Representative (SR) voting/election mechanism: any account can broadcast a `VoteWitnessContract` at any time before the maintenance cycle boundary, and the vote is tallied with exactly the same weight as a vote cast much earlier in the cycle — there is no time-decay, no early-vote incentive, and no penalty for late voting.

### Finding Description
A `VoteWitnessContract` transaction is processed by `VoteWitnessActuator.execute()` / `countVoteAccount()`, which simply overwrites the account's `VotesCapsule` (`clearVotes()`/`clearNewVotes()` then `addNewVotes()`), storing full-weight votes keyed only by the voter's current `tronPower` [1](#0-0) . There is no timestamp-based decay or weighting applied to the vote weight, and validation only checks that `sum <= tronPower` [2](#0-1) .

At the end of each maintenance cycle, `MaintenanceManager.doMaintenance()` calls `countVote(votesStore)`, which iterates every pending `VotesCapsule` in the store and sums `oldVotes`/`newVotes` for each witness with no regard to how recently the vote was cast within the cycle [3](#0-2) . The resulting `countWitness` map is then added directly to each `WitnessCapsule.voteCount`, and the active witness set (`newWits`) is recomputed and swapped in immediately in the same `doMaintenance()` call [4](#0-3) .

Because all votes on-chain are public (visible in `VotesStore`/pending transactions in the block being built), and vote weight is derived from `tronPower` (frozen TRX) that can be built up in advance and only "spent" via a vote transaction at the last possible block before the maintenance boundary, an actor with a large frozen balance can wait to observe all competitors' votes and then cast a decisive vote in the very last block before `doMaintenance()` executes — exactly the "Alice bids 100, Eve bids 110 one block before the end" scenario in the report, but for control of the SR/witness set that produces blocks, earns block rewards, and forms the DPoS consensus committee.

### Impact Explanation
The witness/SR election directly determines who produces blocks, receives block rewards, and participates in on-chain governance (parameter proposals via committee, chain configuration changes). Manipulating this election at the last moment lets a well-funded account intentionally bump out or freeze out witnesses supported by smaller/earlier voters, and can be used to insert a specific candidate into the active SR set (or keep an undesired incumbent) without giving other participants the ability to react, contrary to the intended openness of the voting mechanism. This is a governance/consensus-composition integrity issue rather than a direct fund-theft bug, but it can be leveraged to gain influence over consensus (e.g., who signs blocks) which is a Medium/High-severity concern given DPoS's reliance on fair vote weighting over the cycle.

### Likelihood Explanation
Likelihood is High for any account holding enough frozen TRX (`tronPower`) to matter for the election, since `VoteWitnessContract` is a plain, unprivileged, broadcastable transaction type with no additional access control beyond normal signature/balance checks in `VoteWitnessActuator.validate()` [5](#0-4) . No special permission or SR/witness role is required to exploit this — it is purely a matter of timing a normal vote transaction relative to the known maintenance cycle boundary (which is deterministic and can be tracked via `DynamicPropertiesStore`/`ConsensusDelegate`).

### Recommendation
Short term: introduce a time-based decay/weighting for votes accumulated within a maintenance cycle (e.g., weight = f(time_since_cast, cycle_length)) so that late-cycle votes carry proportionally less influence than early-cycle votes, following the report's suggested mitigation. Alternatively, snapshot/cap the votes counted for a cycle at some cutoff block prior to the maintenance boundary, so last-block votes are deferred to the next cycle rather than immediately decisive. Long term: evaluate more advanced sealed-bid/commit-reveal or continuously time-weighted voting schemes for SR election consistent with current research on manipulation-resistant on-chain voting.

### Proof of Concept
1. Attacker observes on-chain `VotesStore`/pending transactions and computes the current leading vote totals for the witness set just before the next maintenance cycle boundary (tracked via `DynamicPropertiesStore` maintenance time fields).
2. Attacker freezes enough TRX in advance (via `FreezeBalanceContract`) to obtain sufficient `tronPower`, but does not vote yet, keeping the intent hidden.
3. In the last block(s) before `MaintenanceManager.doMaintenance()` fires, attacker submits a `VoteWitnessContract` transaction voting a decisive weight for/against specific witnesses, per `VoteWitnessActuator.execute()`/`countVoteAccount()`.
4. `doMaintenance()` runs, calling `countVote()` which sums all `VotesCapsule` entries (old+new) with no time weighting [3](#0-2) , and the new witness set is computed and swapped in within the same call, changing SR composition based on the attacker's last-second vote with full weight equal to any early voter's weight [6](#0-5) .

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/VoteWitnessActuator.java (L62-150)
```java
  public boolean validate() throws ContractValidateException {
    if (this.any == null) {
      throw new ContractValidateException(ActuatorConstant.CONTRACT_NOT_EXIST);
    }
    if (chainBaseManager == null) {
      throw new ContractValidateException(ActuatorConstant.STORE_NOT_EXIST);
    }
    AccountStore accountStore = chainBaseManager.getAccountStore();
    WitnessStore witnessStore = chainBaseManager.getWitnessStore();
    if (!this.any.is(VoteWitnessContract.class)) {
      throw new ContractValidateException(
          "contract type error, expected type [VoteWitnessContract], real type[" + any
              .getClass() + "]");
    }
    final VoteWitnessContract contract;
    try {
      contract = this.any.unpack(VoteWitnessContract.class);
    } catch (InvalidProtocolBufferException e) {
      logger.debug(e.getMessage(), e);
      throw new ContractValidateException(e.getMessage());
    }
    if (!DecodeUtil.addressValid(contract.getOwnerAddress().toByteArray())) {
      throw new ContractValidateException("Invalid address");
    }
    byte[] ownerAddress = contract.getOwnerAddress().toByteArray();
    String readableOwnerAddress = StringUtil.createReadableString(ownerAddress);

    if (contract.getVotesCount() == 0) {
      throw new ContractValidateException(
          "VoteNumber must more than 0");
    }
    int maxVoteNumber = MAX_VOTE_NUMBER;
    if (contract.getVotesCount() > maxVoteNumber) {
      throw new ContractValidateException(
          "VoteNumber more than maxVoteNumber " + maxVoteNumber);
    }
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

      AccountCapsule accountCapsule = accountStore.get(ownerAddress);
      if (accountCapsule == null) {
        throw new ContractValidateException(
            ACCOUNT_EXCEPTION_STR + readableOwnerAddress + NOT_EXIST_STR);
      }

      long tronPower;
      DynamicPropertiesStore dynamicStore = chainBaseManager.getDynamicPropertiesStore();
      if (dynamicStore.supportAllowNewResourceModel()) {
        tronPower = accountCapsule.getAllTronPower();
      } else {
        tronPower = accountCapsule.getTronPower();
      }

      sum = LongMath
          .checkedMultiply(sum, TRX_PRECISION); //trx -> drop. The vote count is based on TRX
      if (sum > tronPower) {
        throw new ContractValidateException(
            "The total number of votes[" + sum + "] is greater than the tronPower[" + tronPower
                + "]");
      }
    } catch (ArithmeticException e) {
      logger.debug(e.getMessage(), e);
      throw new ContractValidateException(e.getMessage());
    }

    return true;
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

**File:** consensus/src/main/java/org/tron/consensus/dpos/MaintenanceManager.java (L89-152)
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
