### Title
Integer overflow via silent `BigInteger.longValue()` truncation in the vote-reward "vi" accumulator - (File: `chainbase/src/main/java/org/tron/core/store/DelegationStore.java`, `chainbase/src/main/java/org/tron/core/service/RewardViCalService.java`, `chainbase/src/main/java/org/tron/core/service/MortgageService.java`, `actuator/src/main/java/org/tron/core/vm/utils/VoteRewardUtil.java`)

### Summary
java-tron's voter-reward algorithm is a direct analog of the Sushi-Masterchef-style scaled-accumulator pattern described in the report. Instead of a Solidity `uint256`, java-tron uses `BigInteger` for the accumulator (`vi`), scaled by `DECIMAL_OF_VI_REWARD = 10^18` [1](#0-0) . Because `BigInteger` never overflows, the analog of the Solidity `uint256` wraparound occurs at the final step, where the computed `BigInteger` reward is coerced into a primitive `long` via `.longValue()`, which silently returns only the low‑order 64 bits when the value exceeds `Long.MAX_VALUE`, instead of throwing [2](#0-1) .

### Finding Description
Every maintenance cycle, for each witness, the delta reward per vote is accumulated into `vi` (scaled by 1e18) with no guard against tiny vote counts other than exactly zero: [3](#0-2) 

This is invoked unconditionally for every active witness each cycle from `MaintenanceManager.doMaintenance()`, passing the witness's raw `getVoteCount()` with no minimum-vote enforcement: [4](#0-3) 

Block rewards paid to an active (top-27) witness are a **fixed** per-block amount regardless of that witness's current registered vote count: [5](#0-4) 

Since the active-witness set and vote snapshot are only updated once per maintenance cycle, and voters can withdraw/unvote at any time, a witness that remains elected for a cycle but whose voters mass-unvote right after the snapshot can end up crediting `reward * 1e18 / voteCount` with an extremely large `reward` and near-minimal `voteCount`, causing `vi` for that witness to spike disproportionately — precisely mirroring the "attacker deposits 1 wei stake, then large reward" scenario in the report.

When a voter later withdraws/queries their reward, the accumulated `deltaVi` for that witness is multiplied by the voter's own vote count and divided back down by the same `1e18` scale, then truncated to `long`: [2](#0-1) 

The identical unguarded pattern (`BigInteger` multiply/divide then `.longValue()`) is duplicated in the TVM vote precompile path reachable by any smart-contract call (`vote()`/reward query precompiles): [6](#0-5) [7](#0-6) 

and again in the historical/backfill reward computation service: [8](#0-7) 

If the resulting `BigInteger` product exceeds `Long.MAX_VALUE` (~9.2×10^18), `.longValue()` silently returns a wrapped (possibly negative) value instead of throwing, corrupting the reward computation.

### Impact Explanation
A corrupted reward value flows into `adjustAllowance`, which credits/debits the voter's on-chain `allowance` (claimable TRX balance) [9](#0-8) . A wrapped-positive result can mint an unbacked balance increase for the withdrawing account (funds inflation / theft from the reward pool), while a wrapped-negative result is silently dropped by the `amount <= 0` guard in `VoteRewardUtil.adjustAllowance`, permanently freezing the voter's legitimate reward with no path to recovery [10](#0-9) . Because the same `vi` value is shared and read by every voter of that witness (via `getWitnessVi`/`getNewRewardAlgorithmReward`), a single anomalous cycle for one witness can corrupt reward accounting for all its voters going forward.

### Likelihood Explanation
Reaching the overflow requires a witness whose registered vote count is disproportionately small relative to the fixed block rewards it earns for an entire maintenance cycle (e.g., due to mass unvoting by its supporters right after the cycle's vote snapshot, while the witness set itself is only re-elected once per cycle), combined with a voter holding a very large absolute vote/stake for that witness. This is analogous to, but somewhat harder to trigger than, the original report's admin-controlled staking contract, since java-tron's witness election and vote accounting are fully permissionless and continuously changing on a live chain — no admin action can pre-empt a legitimate voter mass-unvoting a witness mid-cycle.

### Recommendation
- Use a checked conversion (e.g., `BigInteger.longValueExact()`) instead of `longValue()` in `MortgageService.computeReward`, `VoteRewardUtil.computeReward`, and `RewardViCalService.getNewRewardAlgorithmReward`, so an out-of-range result throws/reverts rather than silently wrapping.
- Enforce a minimum vote-count threshold before accumulating `vi` in `DelegationStore.accumulateWitnessVi` / `RewardViCalService.accumulateWitnessVi`, similar to the existing zero-vote guard, to prevent disproportionate `deltaVi` spikes.
- Consider reducing/removing the `1e18` scale factor or capping the value of `vi`/`deltaVi` to keep results within a safely representable `long` range end-to-end.

### Proof of Concept
1. A witness `W` is elected into the active top-27 set for cycle `N` based on the previous cycle's vote snapshot.
2. Immediately after the snapshot for cycle `N`, all voters supporting `W` unvote/unfreeze, dropping `W`'s registered vote count to a minimal value (e.g., 1) for the remainder of cycle `N`, while `W` remains active and keeps producing blocks and earning fixed per-block rewards throughout cycle `N` (`MortgageService.payBlockReward` / `payTransactionFeeReward`) [5](#0-4) .
3. At maintenance for cycle `N`, `delegationStore.accumulateWitnessVi(curCycle, W, voteCount≈1)` computes `deltaVi = reward * 1e18 / 1`, producing a disproportionately huge `vi` delta for `W` [3](#0-2) .
4. A large voter (or one whose votes for `W` span the cycle boundary) later calls `withdrawReward`/`queryReward` (directly, or via the TVM `vote`/reward precompile) [6](#0-5) ; `deltaVi.multiply(BigInteger.valueOf(userVote)).divide(DECIMAL_OF_VI_REWARD).longValue()` exceeds `Long.MAX_VALUE`, silently wrapping to a corrupted (possibly negative) `long` reward.
5. The corrupted value is applied via `adjustAllowance`, either crediting an unbacked inflated balance or silently discarding the voter's legitimate reward, depending on the sign of the wrapped result.

### Citations

**File:** chainbase/src/main/java/org/tron/core/store/DelegationStore.java (L20-22)
```java
  public static final long REMARK = -1L;
  public static final int DEFAULT_BROKERAGE = 20;
  public static final BigInteger DECIMAL_OF_VI_REWARD = BigInteger.valueOf(10).pow(18);
```

**File:** chainbase/src/main/java/org/tron/core/store/DelegationStore.java (L133-146)
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
  }
```

**File:** chainbase/src/main/java/org/tron/core/service/MortgageService.java (L69-77)
```java
  public void payBlockReward(byte[] witnessAddress, long value) {
    logger.debug("Pay {} block reward {}.", Hex.toHexString(witnessAddress), value);
    payReward(witnessAddress, value);
  }

  public void payTransactionFeeReward(byte[] witnessAddress, long value) {
    logger.debug("Pay {} transaction fee reward {}.", Hex.toHexString(witnessAddress), value);
    payReward(witnessAddress, value);
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

**File:** chainbase/src/main/java/org/tron/core/service/MortgageService.java (L232-258)
```java
  public void adjustAllowance(byte[] address, long amount) {
    try {
      if (amount <= 0) {
        return;
      }
      adjustAllowance(accountStore, address, amount);
    } catch (BalanceInsufficientException e) {
      logger.error("WithdrawReward error: {}.", e.getMessage());
    }
  }

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
  }
```

**File:** consensus/src/main/java/org/tron/consensus/dpos/MaintenanceManager.java (L96-101)
```java
    if (dynamicPropertiesStore.useNewRewardAlgorithm()) {
      long curCycle = dynamicPropertiesStore.getCurrentCycleNumber();
      consensusDelegate.getAllWitnesses().forEach(witness -> {
        delegationStore.accumulateWitnessVi(curCycle, witness.createDbKey(), witness.getVoteCount());
      });
    }
```

**File:** actuator/src/main/java/org/tron/core/vm/utils/VoteRewardUtil.java (L90-110)
```java
  private static long computeReward(long beginCycle, long endCycle,
                                    AccountCapsule accountCapsule, Repository repository) {
    if (beginCycle >= endCycle) {
      return 0;
    }

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

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1834-1840)
```java
    @Override
    public Pair<Boolean, byte[]> execute(byte[] data) {

      long rewardBalance = VoteRewardUtil.queryReward(
          TransactionTrace.convertToTronAddress(getCallerAddress()), getDeposit());
      return Pair.of(true, longTo32Bytes(rewardBalance));
    }
```

**File:** chainbase/src/main/java/org/tron/core/service/RewardViCalService.java (L155-171)
```java
    long reward = 0;
    if (beginCycle < endCycle) {
      for (Pair<byte[], Long> vote : votes) {
        byte[] srAddress = vote.getKey();
        BigInteger beginVi = getWitnessVi(beginCycle - 1, srAddress);
        BigInteger endVi = getWitnessVi(endCycle - 1, srAddress);
        BigInteger deltaVi = endVi.subtract(beginVi);
        if (deltaVi.signum() <= 0) {
          continue;
        }
        long userVote = vote.getValue();
        reward += deltaVi.multiply(BigInteger.valueOf(userVote))
            .divide(DelegationStore.DECIMAL_OF_VI_REWARD).longValue();
      }
    }
    return reward;

```
