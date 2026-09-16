Based on my research, I found a concrete analog in java-tron's TVM native-contract layer, but I was unable to fully verify the downstream numeric impact of the missing call because I did not get to read `AccountCapsule.oldTronPowerIsNotInitialized()` / `initializeOldTronPower()` / `getAllTronPower()` implementations before running out of tool budget. I present the finding with that caveat.

### Title
Missing TronPower migration/initialization check in TVM `vote()` path causes divergent voting-power accounting vs. the regular VoteWitness transaction path - (File: `actuator/src/main/java/org/tron/core/vm/nativecontract/VoteWitnessProcessor.java`)

### Summary
The Sherlock report describes a state-consistency bug where a dependent operation (`rebalance()`) can be invoked before the required initialization step (`initManagement()`), leaving the contract in an inconsistent state. The java-tron analog is that voting through a normal transaction (`VoteWitnessContract` → `VoteWitnessActuator`) performs a mandatory "legacy TronPower" migration step before computing voting power, while the functionally equivalent path reachable from any deployed smart contract (`vote()` TVM opcode → `VoteWitnessProcessor`) omits that same step.

### Finding Description
`VoteWitnessActuator.execute()` calls `accountCapsule.oldTronPowerIsNotInitialized()` and, if true, `accountCapsule.initializeOldTronPower()` before computing the account's tron power for vote validation: [1](#0-0) 

This same pattern is duplicated in `FreezeBalanceV2Actuator`, `UnfreezeBalanceActuator`, `UnfreezeBalanceV2Actuator`, and `VoteWitnessActuator`, and in the corresponding native-contract processors `FreezeBalanceV2Processor` and `UnfreezeBalanceV2Processor`: [2](#0-1) 

However, `VoteWitnessProcessor` — the code path invoked when a deployed smart contract calls the native `vote()` instruction — never calls `oldTronPowerIsNotInitialized()` / `initializeOldTronPower()`. It goes straight from `repo.getAccount(ownerAddress)` to computing `tronPower = accountCapsule.getAllTronPower()` (or `getTronPower()`), unlike the transaction-level `VoteWitnessActuator`: [3](#0-2) 

This is the TVM opcode wiring that any contract caller (not just a privileged party) can trigger: [4](#0-3) 

Because the migration/initialization of legacy ("old") TronPower fields is only performed via `FreezeBalanceActuator`/`FreezeBalanceV2Actuator`/`UnfreezeBalance*` and `VoteWitnessActuator`, an account that has legacy (pre-FreezeV2) frozen balance and has never triggered one of those specific paths could reach the vote-power calculation in `VoteWitnessProcessor` without the same one-time migration guarantee that the ordinary transaction path enforces — i.e., the "rebalance-before-init" ordering hazard, but for TronPower/vote-weight accounting rather than a vault.

### Impact Explanation
If `getAllTronPower()`/`getTronPower()` produce different results depending on whether `initializeOldTronPower()` has run (this is the part I could not fully confirm from `AccountCapsule.java` within my remaining budget), a contract-triggered vote could compute a TronPower value inconsistent with the value the same account would get through a normal `VoteWitnessContract` transaction. Inconsistent TronPower directly feeds into vote-weight validation (`sum > tronPower` check) and total network vote/reward accounting, so an accounting divergence here could let votes exceed the account's legitimate weight or leave stale/incorrect vote records, which is a Medium-severity integrity issue in the stake/voting subsystem rather than a purely cosmetic one.

### Likelihood Explanation
This path is reachable from any contract call through the standard `vote()` TVM instruction — no elevated privilege needed, only requires that the calling account (the contract itself) has legacy V1-frozen balance that has never passed through one of the four actuators that perform the migration. Given TRON's long operational history with FreezeBalance V1 accounts predating FreezeBalanceV2, such accounts plausibly exist, making the precondition realistic, though the actual account being the "contract" context address (created via CREATE) somewhat narrows practical exploitation to accounts/contracts that specifically accumulated legacy frozen balance before calling vote via TVM.

### Recommendation
Add the same `oldTronPowerIsNotInitialized()` / `initializeOldTronPower()` guard to `VoteWitnessProcessor.execute()` (and audit `DelegateResourceProcessor`/`UnDelegateResourceProcessor`, which also lack this call, for the same reason) so that TVM-triggered operations enforce the identical initialization invariant as their actuator-transaction counterparts.

### Proof of Concept
Not independently executed; based on static code comparison between `VoteWitnessActuator` (calls the init check) and `VoteWitnessProcessor` (does not), reachable via `Program.voteWitness()` from any deployed contract invoking the `vote()` TVM opcode. Full confirmation of the numeric divergence requires reading `AccountCapsule.oldTronPowerIsNotInitialized()`, `initializeOldTronPower()`, `getTronPower()`, and `getAllTronPower()`, which I was not able to complete within the available tool budget — flagging this as an area requiring further verification before treating the impact as certain.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/FreezeBalanceActuator.java (L64-67)
```java
    if (dynamicStore.supportAllowNewResourceModel()
        && accountCapsule.oldTronPowerIsNotInitialized()) {
      accountCapsule.initializeOldTronPower();
    }
```

**File:** actuator/src/main/java/org/tron/core/vm/nativecontract/FreezeBalanceV2Processor.java (L73-82)
```java
  public void execute(FreezeBalanceV2Param param, Repository repo) {
    DynamicPropertiesStore dynamicStore = repo.getDynamicPropertiesStore();

    byte[] ownerAddress = param.getOwnerAddress();
    long frozenBalance = param.getFrozenBalance();
    AccountCapsule accountCapsule = repo.getAccount(ownerAddress);
    if (dynamicStore.supportAllowNewResourceModel()
        && accountCapsule.oldTronPowerIsNotInitialized()) {
      accountCapsule.initializeOldTronPower();
    }
```

**File:** actuator/src/main/java/org/tron/core/vm/nativecontract/VoteWitnessProcessor.java (L39-101)
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
```

**File:** actuator/src/main/java/org/tron/core/vm/program/Program.java (L2059-2093)
```java
  public boolean unfreezeBalanceV2(DataWord unfreezeBalance, DataWord resourceType) {
    Repository repository = getContractState().newRepositoryChild();
    byte[] owner = getContextAddress();

    increaseNonce();
    InternalTransaction internalTx = addInternalTx(null, owner, owner,
        unfreezeBalance.longValue(), null,
        "unfreezeBalanceV2For" + convertResourceToString(resourceType), nonce, null);

    try {
      UnfreezeBalanceV2Param param = new UnfreezeBalanceV2Param();
      param.setOwnerAddress(owner);
      param.setUnfreezeBalance(unfreezeBalance.sValue().longValueExact());
      param.setResourceType(parseResourceCodeV2(resourceType));

      UnfreezeBalanceV2Processor processor = new UnfreezeBalanceV2Processor();
      processor.validate(param, repository);
      long unfreezeExpireBalance = processor.execute(param, repository);
      repository.commit();
      if (unfreezeExpireBalance > 0) {
        increaseNonce();
        addInternalTx(null, owner, owner, unfreezeExpireBalance, null,
            "withdrawExpireUnfreezeWhileUnfreezing", nonce, null);
      }
      return true;
    } catch (ContractValidateException e) {
      logger.warn("TVM UnfreezeBalanceV2: validate failure. Reason: {}", e.getMessage());
    } catch (ArithmeticException e) {
      logger.warn("TVM UnfreezeBalanceV2: balance out of long range.");
    }
    if (internalTx != null) {
      internalTx.reject();
    }
    return false;
  }
```
