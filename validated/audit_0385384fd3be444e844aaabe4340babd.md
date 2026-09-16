### Title
Precision loss in legacy voter reward calculation due to division-before-multiplication in `MortgageService` - (File: chainbase/src/main/java/org/tron/core/service/MortgageService.java)

### Summary
The pre-"new reward algorithm" reward-distribution path in `MortgageService` computes a voter's share of a super representative's cycle reward using `double` division performed **before** the multiplication, mirroring the exact bug class reported in the external Hubble Exchange finding (division applied ahead of multiplication causing avoidable precision loss).

### Finding Description
In the legacy (pre-Vi) reward computation, `computeReward(long cycle, List<Pair<byte[], Long>> votes)` calculates each voter's proportional share as: [1](#0-0) 

```java
double voteRate = (double) userVote / totalVote;
reward += voteRate * totalReward;
```

`userVote / totalVote` is computed first and truncated into the mantissa of a `double`, and the result is *then* multiplied by `totalReward`. This is the same operation-ordering flaw as the reported `AMM.sol` issue: performing division before multiplication discards precision that would be preserved if the multiplication (`userVote * totalReward`) were performed first and divided by `totalVote` last (ideally using integer/`BigInteger` math, as the newer Vi-based algorithm already does a few lines below using `BigInteger.multiply().divide()`) [2](#0-1) .

This legacy path is invoked from `computeReward(long beginCycle, long endCycle, AccountCapsule accountCapsule)` for any cycle prior to `newRewardAlgorithmEffectiveCycle`, via `getOldReward(...)`: [3](#0-2) 

Both `withdrawReward` and `queryReward` (which are reachable by any unprivileged holder submitting a `WithdrawBalanceContract` transaction through `WithdrawBalanceActuator.execute`/`validate`) call `computeReward` on this path: [4](#0-3) [5](#0-4) 

The same pattern (division before multiplication, using `double`) also appears in `payStandbyWitness`/`payReward` for computing each standby witness's pay share and the witness brokerage cut, and in the equivalent `IncentiveManager.reward`: [6](#0-5) [7](#0-6) 

### Impact Explanation
`voteRate * totalReward` computed via `double` after an early division systematically loses precision compared to computing `userVote * totalReward / totalVote` with exact integer/`BigInteger` arithmetic (as the codebase itself does for the newer Vi-based algorithm). This causes the credited `allowance` (and therefore the TRX ultimately withdrawn by `WithdrawBalanceActuator`) to deviate from the mathematically correct amount — an under-crediting of legitimate reward funds, or, depending on rounding direction and repeated compounding across many voters/cycles, cumulative drift in total credited rewards versus the pool actually recorded in `delegationStore`. Because this directly determines account balance changes executed on-chain, it is a funds-accounting correctness issue rather than a purely cosmetic one.

### Likelihood Explanation
Every account with votes cast before `newRewardAlgorithmEffectiveCycle` that calls `withdrawReward`/`queryReward` (via a simple `WithdrawBalanceContract` transaction, or read via `REWARDBALANCE`/`queryReward` precompile path) exercises this exact code. No special privileges are required — it is triggered by any voter withdrawing rewards for old cycles. The precision loss itself is bounded by IEEE-754 double rounding error per addition and is typically very small per call, but occurs unconditionally on this reachable path for all legacy-cycle rewards, so it is a "Medium" rather than an unbounded fund-drain issue.

### Recommendation
Replace the double-based `voteRate = (double) userVote / totalVote; reward += voteRate * totalReward;` computation with exact integer arithmetic performed in the safe order (multiply first, divide last), consistent with the pattern already used for the newer algorithm:
```java
reward += BigInteger.valueOf(userVote)
    .multiply(BigInteger.valueOf(totalReward))
    .divide(BigInteger.valueOf(totalVote))
    .longValueExact();
```
Apply the same fix to the analogous `double`-based share calculations in `payStandbyWitness`, `payReward` (brokerage rate), and `IncentiveManager.reward`.

### Proof of Concept
1. Set up two voters with vote counts that do not divide `totalReward` evenly relative to `totalVote` (e.g., `userVote=1, totalVote=3, totalReward=100`).
2. Call `MortgageService.queryReward`/`withdrawReward` for a cycle prior to `newRewardAlgorithmEffectiveCycle`.
3. Compare the result of the current `(double) userVote / totalVote * totalReward` computation against the exact `BigInteger` computation `userVote * totalReward / totalVote`; observe the discrepancy caused by early division truncation in the double-precision path, exactly analogous to the reported `totalPosSize * maxLiquidationRatio / 1e6` ordering bug.

### Citations

**File:** chainbase/src/main/java/org/tron/core/service/MortgageService.java (L60-87)
```java
    long totalPay = dynamicPropertiesStore.getWitness127PayPerBlock();
    double eachVotePay = (double) totalPay / voteSum;
    for (WitnessCapsule w : witnessStandbys) {
      long pay = (long) (w.getVoteCount() * eachVotePay);
      payReward(w.getAddress().toByteArray(), pay);
      logger.debug("Pay {} stand reward {}.", Hex.toHexString(w.getAddress().toByteArray()), pay);
    }
  }

  public void payBlockReward(byte[] witnessAddress, long value) {
    logger.debug("Pay {} block reward {}.", Hex.toHexString(witnessAddress), value);
    payReward(witnessAddress, value);
  }

  public void payTransactionFeeReward(byte[] witnessAddress, long value) {
    logger.debug("Pay {} transaction fee reward {}.", Hex.toHexString(witnessAddress), value);
    payReward(witnessAddress, value);
  }

  private void payReward(byte[] witnessAddress, long value) {
    long cycle = dynamicPropertiesStore.getCurrentCycleNumber();
    int brokerage = delegationStore.getBrokerage(cycle, witnessAddress);
    double brokerageRate = (double) brokerage / 100;
    long brokerageAmount = (long) (brokerageRate * value);
    value -= brokerageAmount;
    delegationStore.addReward(cycle, witnessAddress, value);
    adjustAllowance(witnessAddress, brokerageAmount);
  }
```

**File:** chainbase/src/main/java/org/tron/core/service/MortgageService.java (L89-134)
```java
  public void withdrawReward(byte[] address) {
    if (!dynamicPropertiesStore.allowChangeDelegation()) {
      return;
    }
    AccountCapsule accountCapsule = accountStore.get(address);
    long beginCycle = delegationStore.getBeginCycle(address);
    long endCycle = delegationStore.getEndCycle(address);
    long currentCycle = dynamicPropertiesStore.getCurrentCycleNumber();
    long reward = 0;
    if (beginCycle > currentCycle || accountCapsule == null) {
      return;
    }
    if (beginCycle == currentCycle) {
      AccountCapsule account = delegationStore.getAccountVote(beginCycle, address);
      if (account != null) {
        return;
      }
    }
    //withdraw the latest cycle reward
    if (beginCycle + 1 == endCycle && beginCycle < currentCycle) {
      AccountCapsule account = delegationStore.getAccountVote(beginCycle, address);
      if (account != null) {
        reward = computeReward(beginCycle, endCycle, account);
        adjustAllowance(address, reward);
        reward = 0;
        logger.info("Latest cycle reward {}, {}.", beginCycle, account.getVotesList());
      }
      beginCycle += 1;
    }
    //
    endCycle = currentCycle;
    if (CollectionUtils.isEmpty(accountCapsule.getVotesList())) {
      delegationStore.setBeginCycle(address, endCycle + 1);
      return;
    }
    if (beginCycle < endCycle) {
      reward += computeReward(beginCycle, endCycle, accountCapsule);
      adjustAllowance(address, reward);
    }
    delegationStore.setBeginCycle(address, endCycle);
    delegationStore.setEndCycle(address, endCycle + 1);
    delegationStore.setAccountVote(endCycle, address, accountCapsule);
    logger.info("Adjust {} allowance {}, now currentCycle {}, beginCycle {}, endCycle {}, "
            + "account vote {}.", Hex.toHexString(address), reward, currentCycle,
        beginCycle, endCycle, accountCapsule.getVotesList());
  }
```

**File:** chainbase/src/main/java/org/tron/core/service/MortgageService.java (L183-185)
```java
      long userVote = vote.getValue();
      double voteRate = (double) userVote / totalVote;
      reward += voteRate * totalReward;
```

**File:** chainbase/src/main/java/org/tron/core/service/MortgageService.java (L199-214)
```java
  private long computeReward(long beginCycle, long endCycle, AccountCapsule accountCapsule) {
    if (beginCycle >= endCycle) {
      return 0;
    }

    long reward = 0;
    long newAlgorithmCycle = dynamicPropertiesStore.getNewRewardAlgorithmEffectiveCycle();
    List<Pair<byte[], Long>> srAddresses = accountCapsule.getVotesList().stream()
        .map(vote -> new Pair<>(vote.getVoteAddress().toByteArray(), vote.getVoteCount()))
        .collect(Collectors.toList());
    if (beginCycle < newAlgorithmCycle) {
      long oldEndCycle = min(endCycle, newAlgorithmCycle,
          dynamicPropertiesStore.disableJavaLangMath());
      reward = getOldReward(beginCycle, oldEndCycle, srAddresses);
      beginCycle = oldEndCycle;
    }
```

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

**File:** actuator/src/main/java/org/tron/core/actuator/WithdrawBalanceActuator.java (L41-70)
```java
    long fee = calcFee();
    final WithdrawBalanceContract withdrawBalanceContract;
    AccountStore accountStore = chainBaseManager.getAccountStore();
    DynamicPropertiesStore dynamicStore = chainBaseManager.getDynamicPropertiesStore();
    MortgageService mortgageService = chainBaseManager.getMortgageService();
    try {
      withdrawBalanceContract = any.unpack(WithdrawBalanceContract.class);
    } catch (InvalidProtocolBufferException e) {
      logger.debug(e.getMessage(), e);
      ret.setStatus(fee, code.FAILED);
      throw new ContractExeException(e.getMessage());
    }

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

**File:** consensus/src/main/java/org/tron/consensus/dpos/IncentiveManager.java (L34-43)
```java
    long totalPay = consensusDelegate.getWitnessStandbyAllowance();
    for (ByteString witness : witnesses) {
      byte[] address = witness.toByteArray();
      long pay = (long) (consensusDelegate.getWitness(address).getVoteCount() * ((double) totalPay
          / voteSum));
      AccountCapsule accountCapsule = consensusDelegate.getAccount(address);
      accountCapsule.setAllowance(accountCapsule.getAllowance() + pay);
      consensusDelegate.saveAccount(accountCapsule);
    }
  }
```
