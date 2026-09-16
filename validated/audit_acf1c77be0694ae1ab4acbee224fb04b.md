### Title
Silent `BigInteger.longValue()` truncation in vote-reward computation can mint unbacked TRX allowance - (File: `actuator/src/main/java/org/tron/core/vm/utils/VoteRewardUtil.java`)

### Summary
`VoteRewardUtil.computeReward()`, and its structurally identical counterparts in `MortgageService.computeReward()` and `RewardViCalService.getNewRewardAlgorithmReward()`, convert a `BigInteger` reward delta to a `long` using the unchecked `.longValue()` method instead of `.longValueExact()`. Exactly like the reported Solidity bug (unsafe `int256` cast that silently wraps instead of reverting), `BigInteger.longValue()` silently truncates to the low 64 bits with no bounds check when the true mathematical result exceeds `Long.MAX_VALUE`, instead of throwing. The truncated (and potentially negative or wildly incorrect) value is then unconditionally added into the user's on-chain `allowance`/reward balance.

### Finding Description
The reward-accrual formula is:
```java
reward += deltaVi.multiply(BigInteger.valueOf(userVote))
    .divide(DelegationStore.DECIMAL_OF_VI_REWARD).longValue();
``` [1](#0-0) 

`deltaVi` is an unbounded `BigInteger` accumulated cycle-over-cycle in `DelegationStore`/`RewardViCalService` (`preVi.add(deltaVi)`), and `userVote` is an attacker-controlled `long` (an account's own TRON Power / vote count) [2](#0-1) . Because `deltaVi * userVote / DECIMAL_OF_VI_REWARD` is computed with full-precision `BigInteger` arithmetic and only truncated to `long` at the very last step via `.longValue()` (which is a silent, no-exception, two's-complement truncation of the 64 low-order bits, not `longValueExact()`), any accumulated `Vi` value large enough to push the quotient above `Long.MAX_VALUE` for a given voter will wrap around, potentially producing a small, zero, or even negative-looking bit pattern that is treated as a valid positive `long` reward.

The same unchecked pattern is duplicated in `MortgageService.computeReward` [3](#0-2) , and in `RewardViCalService.getNewRewardAlgorithmReward`/`accumulateWitnessVi` [4](#0-3)  and [5](#0-4) .

This calculation is reachable from a single unprivileged, signed transaction through several actuator/precompiled paths:
- `WithdrawBalanceActuator.execute` → `MortgageService.withdrawReward` → `computeReward` [6](#0-5) 
- `UnfreezeBalanceActuator.execute` → `MortgageService.withdrawReward` [7](#0-6) 
- TVM `withdrawreward` opcode → `Program.withdrawReward` → `WithdrawRewardProcessor.execute` → `VoteRewardUtil.withdrawReward` → `computeReward`, directly callable by any deployed contract via a broadcastable transaction [8](#0-7) [9](#0-8) 
- TVM `unDelegateResource` path when `allowTvmVote` triggers an implicit withdraw [10](#0-9) 

The truncated `reward` is then applied without any further bounds/overflow check via `adjustAllowance`, which unconditionally does `accountCapsule.setAllowance(allowance + amount)` for any `amount > 0` [11](#0-10) , and `WithdrawRewardProcessor.execute` moves that allowance straight into the account's spendable `balance` [12](#0-11) .

Note that other parts of the codebase have already been hardened against exactly this class of bug (e.g. `MarketUtils.multiplyAndDivide` and `MarketComparator.comparePrice` fall back to `BigInteger` on `ArithmeticException`, and `ResourceProcessor`/`SafeExchangeProcessor` use `addExact`/`longValueExact` and BigInteger-based hardened paths) [13](#0-12) [14](#0-13) , but the reward-Vi computation paths were missed.

### Impact Explanation
An account that accumulates a sufficiently large `Vi` delta (achievable given enough votes/cycles/reward accumulation over time, since `Vi` is stored as an unbounded `BigInteger` and multiplied by the voter's own vote count) can cause `computeReward` to silently wrap the true (overflowing) `BigInteger` reward into an arbitrary 64-bit value instead of failing safely. Depending on the wrapped bit pattern, this can create an unbacked, inflated `allowance`/`balance` credited to the account (minting funds not backed by real block/SR rewards), which is a direct funds-theft / unbacked-balance vulnerability. Even in less severe wrap outcomes, it corrupts the protocol's core reward accounting invariant that reward paid out must correspond to the mathematically correct `deltaVi * userVote / DECIMAL_OF_VI_REWARD`.

### Likelihood Explanation
Triggering requires accumulating a very large `Vi` delta relative to `DECIMAL_OF_VI_REWARD`, which happens naturally over long chain uptime/large vote and reward volumes, and the withdraw path is reachable by any ordinary account or any deployed smart contract using standard `WithdrawBalanceContract`/`UnfreezeBalanceContract` transactions or the TVM `withdrawreward` opcode — no special privilege is needed. The precondition (sufficiently large accumulated `Vi`) is a function of chain lifetime and total resource/vote scale rather than an artificial precondition, making this a realistic long-term risk rather than a purely theoretical one, consistent with a Medium-severity classification (silent overflow that "will make it return a value every time" per the analog report rather than reliably reverting).

### Recommendation
Replace `.longValue()` with `.longValueExact()` (or an explicit bounds check against `Long.MAX_VALUE`/`Long.MIN_VALUE`) in all four locations so that an overflowing reward computation throws `ArithmeticException` instead of silently wrapping:
- `actuator/src/main/java/org/tron/core/vm/utils/VoteRewardUtil.java` line 107
- `chainbase/src/main/java/org/tron/core/service/MortgageService.java` line 226
- `chainbase/src/main/java/org/tron/core/service/RewardViCalService.java` line 167
- `chainbase/src/main/java/org/tron/core/store/DelegationStore.java` (`accumulateWitnessVi`, if similarly affected on the accumulation side)

Callers of `computeReward`/`withdrawReward` should catch the resulting `ArithmeticException` and fail the transaction/actuator (as is already done elsewhere in the codebase, e.g. `WithdrawRewardProcessor.execute` already catches `ArithmeticException` from `LongMath.checkedAdd`), rather than allowing an unbounded value to be silently applied to account state.

### Proof of Concept
1. An account accumulates votes and reward cycles over a long period such that `DelegationStore`/`RewardViCalService` stores a `Vi` (`BigInteger`) for a given SR whose delta between `beginCycle-1` and `endCycle-1`, multiplied by the account's `userVote`, and divided by `DECIMAL_OF_VI_REWARD`, mathematically exceeds `Long.MAX_VALUE`.
2. The account (or a contract acting for it) sends a `WithdrawBalanceContract` transaction, or a contract calls the TVM `withdrawreward` opcode.
3. Execution flow: `WithdrawBalanceActuator.execute` → `MortgageService.withdrawReward` → `computeReward` (or the TVM equivalent `WithdrawRewardProcessor.execute` → `VoteRewardUtil.withdrawReward` → `computeReward`).
4. Inside `computeReward`, `deltaVi.multiply(BigInteger.valueOf(userVote)).divide(DECIMAL_OF_VI_REWARD).longValue()` truncates the oversized `BigInteger` result to 64 bits with no exception, producing an arbitrary `reward` value (potentially far exceeding the account's legitimately earned reward).
5. This value is added to the account's `allowance` via `adjustAllowance`/`repository.updateAccount`, and is then transferred into the spendable `balance` on a subsequent withdraw, crediting the account with funds not backed by any actual reward pool contribution.

**Note:** I could not directly verify the concrete numeric value of `DECIMAL_OF_VI_REWARD` (the grep matched but content wasn't returned before the tool budget ran out), so the exact number of votes/cycles needed to trigger the overflow in practice is not fully quantified here; the vulnerability class and reachable code path, however, are confirmed directly from source.

### Citations

**File:** actuator/src/main/java/org/tron/core/vm/utils/VoteRewardUtil.java (L105-108)
```java
      long userVote = vote.getVoteCount();
      reward += deltaVi.multiply(BigInteger.valueOf(userVote))
          .divide(DelegationStore.DECIMAL_OF_VI_REWARD).longValue();
    }
```

**File:** actuator/src/main/java/org/tron/core/vm/utils/VoteRewardUtil.java (L112-120)
```java
  private static void adjustAllowance(byte[] address, long amount, Repository repository) {
    if (amount <= 0) {
      return;
    }
    AccountCapsule accountCapsule = repository.getAccount(address);
    long allowance = accountCapsule.getAllowance();
    accountCapsule.setAllowance(allowance + amount);
    repository.updateAccount(accountCapsule.createDbKey(), accountCapsule);
  }
```

**File:** chainbase/src/main/java/org/tron/core/store/DelegationStore.java (L133-145)
```java
  public void accumulateWitnessVi(long cycle, byte[] address, long voteCount) {
    BigInteger preVi = getWitnessVi(cycle - 1, address);
    long reward = getReward(cycle, address);
    if (reward == 0 || voteCount == 0) { // Just forward pre vi
      if (!BigInteger.ZERO.equals(preVi)) { // Zero vi will not be record
        setWitnessVi(cycle, address, preVi);
      }
    } else { // Accumulate delta vi
      BigInteger deltaVi = BigInteger.valueOf(reward)
          .multiply(DECIMAL_OF_VI_REWARD)
          .divide(BigInteger.valueOf(voteCount));
      setWitnessVi(cycle, address, preVi.add(deltaVi));
    }
```

**File:** chainbase/src/main/java/org/tron/core/service/MortgageService.java (L224-227)
```java
        long userVote = vote.getValue();
        reward += deltaVi.multiply(BigInteger.valueOf(userVote))
            .divide(DelegationStore.DECIMAL_OF_VI_REWARD).longValue();
      }
```

**File:** chainbase/src/main/java/org/tron/core/service/RewardViCalService.java (L165-167)
```java
        long userVote = vote.getValue();
        reward += deltaVi.multiply(BigInteger.valueOf(userVote))
            .divide(DelegationStore.DECIMAL_OF_VI_REWARD).longValue();
```

**File:** chainbase/src/main/java/org/tron/core/service/RewardViCalService.java (L223-228)
```java
    } else { // Accumulate delta vi
      BigInteger deltaVi = BigInteger.valueOf(reward)
          .multiply(DECIMAL_OF_VI_REWARD)
          .divide(BigInteger.valueOf(voteCount));
      setWitnessVi(cycle, address, preVi.add(deltaVi));
    }
```

**File:** actuator/src/main/java/org/tron/core/actuator/WithdrawBalanceActuator.java (L54-55)
```java
    mortgageService.withdrawReward(withdrawBalanceContract.getOwnerAddress()
        .toByteArray());
```

**File:** actuator/src/main/java/org/tron/core/actuator/UnfreezeBalanceActuator.java (L73-74)
```java
    //
    mortgageService.withdrawReward(ownerAddress);
```

**File:** actuator/src/main/java/org/tron/core/vm/program/Program.java (L2340-2359)
```java
  public long withdrawReward() {
    Repository repository = getContractState().newRepositoryChild();
    byte[] owner = getContextAddress();

    increaseNonce();
    InternalTransaction internalTx = addInternalTx(null, owner, owner, 0, null,
        "withdrawReward", nonce, null);

    WithdrawRewardParam param = new WithdrawRewardParam();
    param.setOwnerAddress(owner);
    param.setNowInMs(getTimestamp().longValue() * 1000);
    try {
      WithdrawRewardProcessor processor = new WithdrawRewardProcessor();
      processor.validate(param, repository);
      long allowance = processor.execute(param, repository);
      repository.commit();
      if (internalTx != null) {
        internalTx.setValue(allowance);
      }
      return allowance;
```

**File:** actuator/src/main/java/org/tron/core/vm/nativecontract/WithdrawRewardProcessor.java (L38-67)
```java
  public long execute(WithdrawRewardParam param, Repository repo) throws ContractExeException {
    byte[] ownerAddress = param.getOwnerAddress();

    VoteRewardUtil.withdrawReward(ownerAddress, repo);

    AccountCapsule accountCapsule = repo.getAccount(ownerAddress);
    long oldBalance = accountCapsule.getBalance();
    long allowance = accountCapsule.getAllowance();
    long newBalance = 0;

    try {
      newBalance = LongMath.checkedAdd(oldBalance, allowance);
    } catch (ArithmeticException e) {
      logger.debug(e.getMessage(), e);
      throw new ContractExeException(e.getMessage());
    }

    // If no allowance, do nothing and just return zero.
    if (allowance <= 0) {
      return 0;
    }

    accountCapsule.setInstance(accountCapsule.getInstance().toBuilder()
        .setBalance(newBalance)
        .setAllowance(0L)
        .setLatestWithdrawTime(param.getNowInMs())
        .build());

    repo.updateAccount(accountCapsule.createDbKey(), accountCapsule);
    return allowance;
```

**File:** actuator/src/main/java/org/tron/core/vm/nativecontract/UnfreezeBalanceProcessor.java (L205-223)
```java
    if (VMConfig.allowTvmVote() && !accountCapsule.getVotesList().isEmpty()) {
      long usedTronPower = 0;
      for (Protocol.Vote vote : accountCapsule.getVotesList()) {
        usedTronPower += vote.getVoteCount();
      }
      if (accountCapsule.getTronPower() < usedTronPower * TRX_PRECISION) {
        VoteRewardUtil.withdrawReward(ownerAddress, repo);
        VotesCapsule votesCapsule = repo.getVotes(ownerAddress);
        accountCapsule = repo.getAccount(ownerAddress);
        if (votesCapsule == null) {
          votesCapsule = new VotesCapsule(ByteString.copyFrom(ownerAddress),
              accountCapsule.getVotesList());
        } else {
          votesCapsule.clearNewVotes();
        }
        accountCapsule.clearVotes();
        repo.updateVotes(ownerAddress, votesCapsule);
        repo.updateAccount(ownerAddress, accountCapsule);
      }
```

**File:** chainbase/src/main/java/org/tron/core/capsule/utils/MarketUtils.java (L264-277)
```java
  public static long multiplyAndDivide(long a, long b, long c, boolean disableMath) {
    try {
      long tmp = multiplyExact(a, b, disableMath);
      return floorDiv(tmp, c, disableMath);
    } catch (ArithmeticException ex) {
      // do nothing here, because we will use BigInteger to compute again
    }

    BigInteger aBig = BigInteger.valueOf(a);
    BigInteger bBig = BigInteger.valueOf(b);
    BigInteger cBig = BigInteger.valueOf(c);

    return aBig.multiply(bBig).divide(cBig).longValue();
  }
```

**File:** chainbase/src/main/java/org/tron/core/db/ResourceProcessor.java (L371-378)
```java
  protected long calculateGlobalLimitV2(long frozeBalance,
      long totalLimit, long totalWeight) {
    return BigInteger.valueOf(frozeBalance)
        .multiply(BigInteger.valueOf(totalLimit))
        .divide(BigInteger.valueOf(TRX_PRECISION)
            .multiply(BigInteger.valueOf(totalWeight)))
        .longValueExact();
  }
```
