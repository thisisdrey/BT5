### Title
Unchecked/truncating arithmetic in DPoS vote-reward accounting can corrupt `allowance`/`balance` accounting - (File: `chainbase/src/main/java/org/tron/core/service/MortgageService.java`)

### Summary
`MortgageService` and its VM-facing counterpart `VoteRewardUtil` compute a voter's withdrawable reward by accumulating `BigInteger` deltas (`Vi`) and converting the final value to a `long` using `BigInteger.longValue()` instead of `longValueExact()`, then add it into a plain `long reward` accumulator with unchecked `+=`. The resulting `reward` is fed unchecked into `adjustAllowance`, which itself sets `account.setAllowance(allowance + amount)` using plain `long` addition instead of the `addExact`/`LongMath.checkedAdd` guards used elsewhere in the same codebase (e.g. `Commons.adjustBalance`, `WithdrawRewardProcessor.execute`). This is the same bug class as the external report: math paths that are "trusted" not to overflow are left unchecked, while parallel/sibling code paths in the very same project already recognize the need for exact/checked arithmetic (`RepositoryImpl.getUsage`, `RepositoryImpl.calculateGlobalEnergyLimit` under `hardenResourceCalculation`, and `WithdrawRewardProcessor.execute` using `LongMath.checkedAdd`).

### Finding Description
- `MortgageService.computeReward(long, long, AccountCapsule)` accumulates reward as: [1](#0-0) 
using `deltaVi.multiply(BigInteger.valueOf(userVote)).divide(DECIMAL_OF_VI_REWARD).longValue()` — `longValue()` silently truncates/wraps if the `BigInteger` result exceeds the `long` range, rather than throwing like `longValueExact()`.
- The identical pattern is duplicated in the VM (TVM `WithdrawRewardContract`) path: [2](#0-1) 
- The resulting value is written into the account's `allowance` with an unguarded addition: [3](#0-2) 
This is inconsistent with `Commons.adjustBalance`, which uses `addExact` for the analogous `balance` field: [4](#0-3) 
- `WithdrawBalanceActuator.execute()` also performs the final allowance→balance merge with plain addition (`oldBalance + allowance`), even though its own `validate()` uses `LongMath.checkedAdd` and the newer VM equivalent `WithdrawRewardProcessor.execute()` uses `LongMath.checkedAdd` for the same operation: [5](#0-4) [6](#0-5) 
Because `mortgageService.withdrawReward(...)` runs inside `execute()` (after `validate()`'s overflow check already passed on the pre-withdraw allowance), any additional allowance/reward accrued by `withdrawReward()` between validate and execute is added to `balance` with no overflow protection at all in `WithdrawBalanceActuator`.

### Impact Explanation
If the reward accumulation (`Vi` deltas driven by `getReward(cycle, address)` and `witness vote counts`, both attacker-influenceable via freeze/vote/unfreeze/reward timing across many maintenance cycles) can be pushed to produce a `BigInteger` product exceeding `Long.MAX_VALUE` before the final divide, the silent truncation in `.longValue()` can produce an arbitrary (including negative) `long` reward value. This value flows into `adjustAllowance` and ultimately into `AccountCapsule.balance` via `WithdrawBalanceActuator`/`WithdrawRewardProcessor`, which is a case of "unbacked balance" — i.e., an account's spendable TRX balance derived from a corrupted/overflowed computation rather than backed value, or resulting in negative/incorrect allowance impacting other stakers' rewards.

### Likelihood Explanation
Reward accrual is driven only by normal, permissionless actions — freezing balance for TRON Power, voting for SRs, and letting reward accumulate over maintenance cycles (`MaintenanceManager.doMaintenance`) — combined with standard `WithdrawBalanceContract`/`WithdrawRewardContract` calls, so it is reachable by any unprivileged account holder with no special permission. Reaching the actual overflow threshold requires the underlying `Vi`/vote-count/reward inputs to combine such that `deltaVi * userVote` exceeds `Long.MAX_VALUE` prior to division, which is bounded in practice by real network totals (reward pools and TRX supply); this bounds the practical likelihood, but the missing `longValueExact()`/`checkedAdd()` guard is a genuine defense-in-depth gap directly analogous to the external report, since the code base itself demonstrates awareness of exactly this issue (`longValueExact()` and `LongMath.checkedAdd()` are used in sibling reward/resource-calculation code paths but omitted here).

### Recommendation
- Replace `BigInteger.longValue()` with `BigInteger.longValueExact()` (or explicit bounds checking) in `MortgageService.computeReward` and `VoteRewardUtil.computeReward` so an overflowing conversion throws instead of silently wrapping.
- Use `Maths.addExact`/`LongMath.checkedAdd` in `MortgageService.adjustAllowance` for the `allowance + amount` computation, matching the pattern already used in `Commons.adjustBalance`.
- Replace the unchecked `oldBalance + allowance` in `WithdrawBalanceActuator.execute()` with `LongMath.checkedAdd`, matching `WithdrawRewardProcessor.execute()`, and re-validate the sum after `mortgageService.withdrawReward()` runs (since it can change `allowance` after `validate()`'s check).

### Proof of Concept
Not concretely demonstrable purely from static analysis: triggering the overflow requires driving `deltaVi.multiply(userVote)` (accumulated `Vi` over many maintenance cycles times an attacker's vote weight) past `Long.MAX_VALUE` before division by `DECIMAL_OF_VI_REWARD`, which needs a live chain-state/economic simulation (reward pool sizes, vote-count manipulation across cycles) to confirm feasibility under real supply constraints — this would need to be validated in a running testnet/harness rather than asserted from code inspection alone.

### Citations

**File:** chainbase/src/main/java/org/tron/core/service/MortgageService.java (L215-227)
```java
    if (beginCycle < endCycle) {
      for (Pair<byte[], Long>  vote : srAddresses) {
        byte[] srAddress = vote.getKey();
        BigInteger beginVi = delegationStore.getWitnessVi(beginCycle - 1, srAddress);
        BigInteger endVi = delegationStore.getWitnessVi(endCycle - 1, srAddress);
        BigInteger deltaVi = endVi.subtract(beginVi);
        if (deltaVi.signum() <= 0) {
          continue;
        }
        long userVote = vote.getValue();
        reward += deltaVi.multiply(BigInteger.valueOf(userVote))
            .divide(DelegationStore.DECIMAL_OF_VI_REWARD).longValue();
      }
```

**File:** chainbase/src/main/java/org/tron/core/service/MortgageService.java (L243-257)
```java
  public void adjustAllowance(AccountStore accountStore, byte[] accountAddress, long amount)
      throws BalanceInsufficientException {
    AccountCapsule account = accountStore.getUnchecked(accountAddress);
    long allowance = account.getAllowance();
    if (amount == 0) {
      return;
    }

    if (amount < 0 && allowance < -amount) {
      throw new BalanceInsufficientException(
          String.format("%s insufficient balance, amount: %d, allowance: %d",
              StringUtil.createReadableString(accountAddress), amount, allowance));
    }
    account.setAllowance(allowance + amount);
    accountStore.put(account.createDbKey(), account);
```

**File:** actuator/src/main/java/org/tron/core/vm/utils/VoteRewardUtil.java (L96-109)
```java
    long reward = 0;
    for (Protocol.Vote vote : accountCapsule.getVotesList()) {
      byte[] srAddress = vote.getVoteAddress().toByteArray();
      BigInteger beginVi = repository.getDelegationStore().getWitnessVi(beginCycle - 1, srAddress);
      BigInteger endVi = repository.getDelegationStore().getWitnessVi(endCycle - 1, srAddress);
      BigInteger deltaVi = endVi.subtract(beginVi);
      if (deltaVi.signum() <= 0) {
        continue;
      }
      long userVote = vote.getVoteCount();
      reward += deltaVi.multiply(BigInteger.valueOf(userVote))
          .divide(DelegationStore.DECIMAL_OF_VI_REWARD).longValue();
    }
    return reward;
```

**File:** chainbase/src/main/java/org/tron/common/utils/Commons.java (L80-95)
```java
  public static void adjustBalance(AccountStore accountStore, AccountCapsule account, long amount,
                                   boolean useStrict)
      throws BalanceInsufficientException {

    long balance = account.getBalance();
    if (amount == 0) {
      return;
    }

    if (amount < 0 && balance < -amount) {
      throw new BalanceInsufficientException(
          String.format("%s insufficient balance, balance: %d, amount: %d",
              StringUtil.createReadableString(account.createDbKey()), balance, -amount));
    }
    account.setBalance(addExact(balance, amount, useStrict));
    accountStore.put(account.getAddress().toByteArray(), account);
```

**File:** actuator/src/main/java/org/tron/core/actuator/WithdrawBalanceActuator.java (L54-70)
```java
    mortgageService.withdrawReward(withdrawBalanceContract.getOwnerAddress()
        .toByteArray());

    AccountCapsule accountCapsule = accountStore.
        get(withdrawBalanceContract.getOwnerAddress().toByteArray());
    long oldBalance = accountCapsule.getBalance();
    long allowance = accountCapsule.getAllowance();

    long now = dynamicStore.getLatestBlockHeaderTimestamp();
    accountCapsule.setInstance(accountCapsule.getInstance().toBuilder()
        .setBalance(oldBalance + allowance)
        .setAllowance(0L)
        .setLatestWithdrawTime(now)
        .build());
    accountStore.put(accountCapsule.createDbKey(), accountCapsule);
    ret.setWithdrawAmount(allowance);
    ret.setStatus(fee, code.SUCESS);
```

**File:** actuator/src/main/java/org/tron/core/vm/nativecontract/WithdrawRewardProcessor.java (L38-68)
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
  }
```
