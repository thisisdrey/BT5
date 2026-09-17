I found a strong analog reachable from a single TVM opcode invocation within a signed smart-contract transaction: `Program.voteWitness()` calls into `VoteWitnessProcessor.execute()`, which dereferences `accountCapsule` (from `repo.getAccount(ownerAddress)`) without any null check.### Title
Null pointer dereference in `VoteWitnessProcessor.execute` via TVM `VOTEWITNESS` opcode - (File: actuator/src/main/java/org/tron/core/vm/nativecontract/VoteWitnessProcessor.java)

### Summary
The TVM native `VOTEWITNESS` opcode path (`Program.voteWitness()` → `VoteWitnessProcessor.validate()`/`execute()`) resolves the caller/contract address as the "voter address" via `Repository.getAccount(ownerAddress)`, and then unconditionally dereferences the returned `AccountCapsule` without checking for `null`, unlike the analogous transaction-level `VoteWitnessActuator`, which explicitly null-checks the owner account before use.

### Finding Description
`Program.voteWitness()` builds a `VoteWitnessParam` with `param.setVoterAddress(owner)` where `owner = getContextAddress()` (the currently executing contract's address), then calls: [1](#0-0) 

`VoteWitnessProcessor.validate()` only checks the vote count against `MAX_VOTE_NUMBER`; it performs no check that the account for `param.getVoterAddress()` exists in the repository: [2](#0-1) 

`VoteWitnessProcessor.execute()` then fetches the account and immediately dereferences it (`accountCapsule.getVotesList()` as a fallback constructor argument, and unconditionally `accountCapsule.clearVotes()`, `accountCapsule.getAllTronPower()`/`getTronPower()`, `accountCapsule.addVotes(...)`) without any null check: [3](#0-2) 

This is architecturally identical to the CVE-2024-7006 bug class (a function trusting that a preceding lookup always returns a non-null object and dereferencing it directly, causing a crash on the unhappy path). By contrast, the equivalent externally-facing `VoteWitnessActuator.validate()` explicitly guards against this: [4](#0-3) 

The account whose existence is being assumed is the *executing contract's own account* (`getContextAddress()`), i.e., the contract that is currently running the `VOTEWITNESS` opcode. In the "normal" case a contract account must exist (it was created and funded to be invoked), so this path is not trivially reachable in the common case. However, `execute()` is reached even when `validate()` passes trivially (e.g., an empty votes list satisfies `size() > MAX_VOTE_NUMBER` as false), and the code path around `withdrawReward`/nonce/`suicide`/self-destruct/re-entrancy handling in `Program.java` creates windows where an account can be marked for deletion, self-destructed, or otherwise transiently absent from the repository cache/store before a `VOTEWITNESS` opcode executes within the same transaction (e.g., a contract calling `SUICIDE`/`selfdestruct` semantics followed by further opcode execution in nested/pre/post logic, or a delegatecall context where `getContextAddress()` resolves to a different address than the one actually funded). Because `Repository.getAccount()` can legitimately return `null` (as demonstrated by its own null-tolerant implementation and by every other native processor and actuator in this codebase explicitly guarding against it), an attacker who can arrange for the account backing `getContextAddress()` to be absent at the moment `VOTEWITNESS` executes triggers an unguarded `NullPointerException`.

### Impact Explanation
An `NullPointerException` thrown here propagates as a runtime exception out of `execute()`. `Program.voteWitness()` only catches `ContractValidateException`, `ContractExeException`, and `ArithmeticException` — it does **not** catch `NullPointerException`: [5](#0-4) 

An uncaught `NullPointerException` inside TVM opcode execution is not a normal "revert" — it is an unexpected runtime exception that can propagate up through the VM execution stack in ways not handled by the transaction's normal exception-to-revert conversion, risking an uncaught exception during block application (denial of service / node crash on all nodes replaying the same transaction), which matches the "node crash or halt" acceptance criterion.

### Likelihood Explanation
Triggering requires a crafted smart contract that executes the `VOTEWITNESS` opcode (gated behind `VMConfig.allowTvmVote()`, i.e., requires the TVM Vote proposal to be enabled by the committee) under a state where the contract's own backing account is not resolvable via `Repository.getAccount()` at the moment the opcode runs. This is a narrower, more state-dependent trigger than a straightforward missing-null-check bug reachable directly from calldata; further dynamic-execution tracing (e.g., through `SUICIDE`/self-destruct interactions or delegatecall context confusion) would be needed to conclusively demonstrate a concrete, unprivileged reachable sequence, which was not verified with certainty in this analysis.

### Recommendation
Add an explicit null check in `VoteWitnessProcessor` (either in `validate()` or at the top of `execute()`), mirroring `VoteWitnessActuator.validate()`:
```java
AccountCapsule accountCapsule = repo.getAccount(ownerAddress);
if (accountCapsule == null) {
  throw new ContractValidateException(ACCOUNT_EXCEPTION_STR + ... + NOT_EXIST_STR);
}
```
and perform this check before any use of `accountCapsule` in both `validate()` and `execute()`.

### Proof of Concept
Not independently constructed/executed in this analysis; a concrete PoC would require crafting a contract that invokes the `VOTEWITNESS` opcode in a call context where `getContextAddress()`'s backing account has been removed or is unresolvable in the active `Repository` chain at execution time, which was not verified to be reachable in this static review.

### Citations

**File:** actuator/src/main/java/org/tron/core/vm/program/Program.java (L2301-2326)
```java
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
      if (internalTx != null) {
        internalTx.setExtra(param.toJsonStr());
      }

      VoteWitnessProcessor processor = new VoteWitnessProcessor();
      processor.validate(param, repository);
      processor.execute(param, repository);
      repository.commit();
      return true;
```

**File:** actuator/src/main/java/org/tron/core/vm/program/Program.java (L2327-2337)
```java
    } catch (ContractValidateException e) {
      logger.warn("TVM VoteWitness: validate failure. Reason: {}", e.getMessage());
    } catch (ContractExeException e) {
      logger.warn("TVM VoteWitness: execute failure. Reason: {}", e.getMessage());
    } catch (ArithmeticException e) {
      logger.warn("TVM VoteWitness: int or long out of range. caused by: {}", e.getMessage());
    }
    if (internalTx != null) {
      internalTx.reject();
    }
    return false;
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

**File:** actuator/src/main/java/org/tron/core/vm/nativecontract/VoteWitnessProcessor.java (L39-53)
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

```

**File:** actuator/src/main/java/org/tron/core/actuator/VoteWitnessActuator.java (L123-127)
```java
      AccountCapsule accountCapsule = accountStore.get(ownerAddress);
      if (accountCapsule == null) {
        throw new ContractValidateException(
            ACCOUNT_EXCEPTION_STR + readableOwnerAddress + NOT_EXIST_STR);
      }
```
