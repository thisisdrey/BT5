### Title
Empty-array vote bypass in TVM native `voteWitness` allows silent wiping of stake votes without validation - (`actuator/src/main/java/org/tron/core/vm/nativecontract/VoteWitnessProcessor.java`)

### Summary
The externally reported issue concerns missing checks for empty array parameters in Solidity setter functions that can lead to unexpected/unvalidated state changes. The closest reachable analog in java-tron is the TVM-exposed native "vote witness" precompile path, which — unlike the transaction-level `VoteWitnessActuator` — performs no check that the caller-supplied vote array is non-empty before clearing and overwriting the account's vote state.

### Finding Description
The transaction-level actuator `VoteWitnessActuator.validate()` explicitly rejects an empty votes list: [1](#0-0) 

However, the TVM opcode/native-contract equivalent, invoked from `Program.voteWitness(...)` (a smart-contract-reachable precompile), builds a `VoteWitnessParam` directly from caller-controlled memory arrays and never enforces a minimum vote count: [2](#0-1) 

`VoteWitnessProcessor.validate()` only checks the upper bound (`MAX_VOTE_NUMBER`) and does not reject a zero-length `votes` list: [3](#0-2) 

`VoteWitnessProcessor.execute()` unconditionally clears the account's existing votes (`accountCapsule.clearVotes()`, `votesCapsule.clearNewVotes()`) before iterating the (possibly empty) vote list, then commits the account/votes state regardless of whether any votes were actually supplied: [4](#0-3) 

This mirrors exactly the reported bug class: a public entry point takes an array parameter with no explicit empty-array guard, and the downstream logic proceeds to mutate on-chain state (vote/stake bookkeeping) based on that unchecked array, diverging silently from the equivalent validated transaction path (`VoteWitnessActuator`) which explicitly disallows empty vote submissions with the message "VoteNumber must more than 0".

### Impact Explanation
Calling this TVM opcode with `witnessArrayLength == 0` (and matching `amountArrayLength == 0`) passes both the memory-length consistency check and `MAX_VOTE_NUMBER` check, then wipes all of the caller's existing witness votes without performing any of the balance/tron-power/witness-existence checks that the transaction-level actuator performs for normal votes, and without any explicit rejection for supplying zero votes. This creates an inconsistency between two code paths that are supposed to implement the same on-chain semantics (vote submission), where the TVM path silently allows an operation ("submit zero votes") that the transaction path explicitly forbids. This can result in unexpected witness vote/stake-accounting state changes purely from contract logic that omits an explicit non-empty check, which is the exact class of issue flagged in the source report (unexpected outcome from unguarded empty-array input).

### Likelihood Explanation
This path is reachable by any deployed smart contract executing the corresponding TVM voteWitness opcode with attacker/caller-controlled array-length parameters, requiring only a normal contract call — no special privilege, SR/witness status, or elevated permission is needed. Any account can construct a contract that calls this opcode with zero-length arrays.

### Recommendation
Add an explicit check in `VoteWitnessProcessor.validate()` (mirroring `VoteWitnessActuator`) rejecting `param.getVotes().isEmpty()` before allowing `execute()` to clear and commit vote state, ensuring parity between the transaction-level and TVM-native vote-submission code paths.

### Proof of Concept
1. Deploy a contract that invokes the TVM `voteWitness` precompile opcode via `Program.voteWitness(witnessArrayOffset, 0, amountArrayOffset, 0)` (zero-length witness and amount arrays).
2. `Program.voteWitness` passes the length-consistency check (`0 == 0`) and the length-equality check (`0 == 0`).
3. `VoteWitnessProcessor.validate()` is called with an empty `votes` list; `0 > MAX_VOTE_NUMBER` is false, so validation passes without error (contrast with `VoteWitnessActuator.validate()`, which would throw `"VoteNumber must more than 0"` for the same input via the standard transaction path).
4. `VoteWitnessProcessor.execute()` clears `accountCapsule`'s existing votes and `votesCapsule`'s new votes, iterates zero entries, and commits the (now-cleared) vote state — silently discarding the account's prior votes with no explicit "empty array" rejection, unlike the equivalent transaction-based flow.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/VoteWitnessActuator.java (L89-92)
```java
    if (contract.getVotesCount() == 0) {
      throw new ContractValidateException(
          "VoteNumber must more than 0");
    }
```

**File:** actuator/src/main/java/org/tron/core/vm/program/Program.java (L2279-2317)
```java
  public boolean voteWitness(int witnessArrayOffset, int witnessArrayLength,
      int amountArrayOffset, int amountArrayLength) {
    Repository repository = getContractState().newRepositoryChild();
    byte[] owner = getContextAddress();

    increaseNonce();
    InternalTransaction internalTx = addInternalTx(null, owner, null, 0, null,
        "voteWitness", nonce, null);

    if (memoryLoad(witnessArrayOffset).intValueSafe() != witnessArrayLength ||
        memoryLoad(amountArrayOffset).intValueSafe() != amountArrayLength) {
      logger.warn("TVM VoteWitness: memory array length do not match length parameter!");
      throw new BytecodeExecutionException(
          "TVM VoteWitness: memory array length do not match length parameter!");
    }

    if (witnessArrayLength != amountArrayLength) {
      logger.warn("TVM VoteWitness: witness array length {} does not match amount array length {}",
          witnessArrayLength, amountArrayLength);
      return false;
    }

    try {
      VoteWitnessParam param = new VoteWitnessParam();
      param.setVoterAddress(owner);
      byte[] witnessArrayData = memoryChunk(
          addExact(witnessArrayOffset, DataWord.WORD_SIZE,  VMConfig.disableJavaLangMath()),
          multiplyExact(witnessArrayLength, DataWord.WORD_SIZE,  VMConfig.disableJavaLangMath()));
      byte[] amountArrayData = memoryChunk(
          addExact(amountArrayOffset, DataWord.WORD_SIZE,  VMConfig.disableJavaLangMath()),
          multiplyExact(amountArrayLength, DataWord.WORD_SIZE,  VMConfig.disableJavaLangMath()));

      for (int i = 0; i < witnessArrayLength; i++) {
        DataWord witness = new DataWord(Arrays.copyOfRange(witnessArrayData,
            i * DataWord.WORD_SIZE, (i + 1) * DataWord.WORD_SIZE));
        DataWord amount = new DataWord(Arrays.copyOfRange(amountArrayData,
            i * DataWord.WORD_SIZE, (i + 1) * DataWord.WORD_SIZE));
        param.addVote(witness.toTronAddress(), amount.sValue().longValueExact());
      }
```

**File:** actuator/src/main/java/org/tron/core/vm/nativecontract/VoteWitnessProcessor.java (L28-37)
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
```

**File:** actuator/src/main/java/org/tron/core/vm/nativecontract/VoteWitnessProcessor.java (L39-111)
```java
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
