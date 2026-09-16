## Title
TVM-triggered vote reward withdrawal reads raw `DelegationStore` Vi checkpoints without the pre-effective-cycle split, letting new-algorithm voters be credited historical rewards they never earned - (`actuator/src/main/java/org/tron/core/vm/utils/VoteRewardUtil.java`)

### Summary
The report describes a reward-accounting class of bug: when checkpoint/integral bookkeeping for a reward-per-share scheme is skipped or diverges from the canonical path during a state transition (their "shutdown"), a user's balance/vote can change while the stored reward baseline is stale, letting that user later be credited reward accrued before their real participation window — a form of reward theft from other stakers. java-tron has an analogous two-track reward-integral (`Vi`) system for SR voting rewards, and the two tracks are **not kept consistent** between the legacy actuator path and the TVM native-contract path.

### Finding Description
java-tron computes per-witness reward integrals (`Vi`) that accumulate every maintenance cycle, and a voter's earned reward is `Σ (Vi[endCycle-1] - Vi[beginCycle-1]) * userVote`.

- The canonical (legacy actuator) path in `MortgageService.computeReward(long beginCycle, long endCycle, AccountCapsule)` explicitly special-cases cycles that predate `dynamicPropertiesStore.getNewRewardAlgorithmEffectiveCycle()`: it calls `getOldReward(...)`, which in turn calls `RewardViCalService.getNewRewardAlgorithmReward(...)`. That method blocks on `lock.await()` until the one-time backfill job (`RewardViCalService.startRewardCal()`) has populated the *separate* `RewardViStore` with correct historical Vi values for every cycle before the new-algorithm effective cycle: [1](#0-0) [2](#0-1) 

- The TVM/native-contract path, `VoteRewardUtil.computeReward`, used by `WithdrawRewardProcessor`, `VoteWitnessProcessor`, and `UnfreezeBalanceV2Processor`, does **not** perform this split. It reads `repository.getDelegationStore().getWitnessVi(cycle, srAddress)` directly for the *entire* `[beginCycle, endCycle)` range, with no reference to `getNewRewardAlgorithmEffectiveCycle()`, `RewardViCalService`, or `RewardViStore`: [3](#0-2) 

- `DelegationStore.getWitnessVi` only reads/writes the live `DelegationStore` DB, which is populated incrementally going forward from the moment the new algorithm went live (`MaintenanceManager.doMaintenance()` calling `delegationStore.accumulateWitnessVi(...)` once `useNewRewardAlgorithm()` is true). It has no knowledge of the backfilled historical Vi values that live only in the separate `RewardViStore`: [4](#0-3) [5](#0-4) 

- Consequently, for any account whose `beginCycle` (per `Repository.getBeginCycle`/`DelegationStore` account-vote bookkeeping) predates the new-algorithm effective cycle, `getWitnessVi(beginCycle-1, srAddress)` on the `DelegationStore` returns `BigInteger.ZERO` (no entry exists there for old cycles), while `getWitnessVi(endCycle-1, srAddress)` can be a large, fully-accumulated value if `endCycle` is well into the live-accumulation era. `deltaVi = endVi - beginVi` is then inflated to cover the witness's *entire* historical reward stream rather than only the caller's real holding period, which is exactly the "checkpoint not updated → new/late accounts get more reward than they earned, possible theft of rewards" pattern from the reference report.

### Impact Explanation
Any account with unresolved votes spanning the pre-/post-new-reward-algorithm boundary that triggers reward withdrawal through the TVM path (`WITHDRAWREWARD`, `VOTEWITNESS`, or unfreeze-v2 native opcodes, reachable from any deployed contract once `allowTvmVote` is enabled) can be credited reward far beyond its rightful share, drained from the shared `Allowance`/reward pool that ultimately reduces balances legitimately owed to other voters — an unbacked-balance / theft-of-funds condition on live mainnet economics (this logic gate, `allowTvmVote`, is already enabled on mainnet).

### Likelihood Explanation
Reachable by any unprivileged account: simply hold votes recorded before `ALLOW_NEW_REWARD`'s effective cycle (already the case for every account that voted before that governance proposal activated) and call the TVM `withdrawReward`/`voteWitness`/`unfreezeBalanceV2` native opcode instead of the legacy actuator equivalent. No special privilege, timing race, or SR collusion is required — only calling the already-live TVM entry point that skips the historical-Vi reconciliation that the legacy path performs.

### Recommendation
Make `VoteRewardUtil.computeReward` mirror `MortgageService.computeReward`: branch on `dynamicPropertiesStore.getNewRewardAlgorithmEffectiveCycle()`, and for cycles before that boundary, route through the same `RewardViCalService`/`RewardViStore`-backed historical lookup (with the same completion-lock wait) instead of reading raw `DelegationStore` Vi entries. Add a regression test that withdraws reward via a TVM contract for an account whose vote predates the new-algorithm effective cycle and asserts the amount matches the legacy `MortgageService.withdrawReward` result for an identical voting history.

### Proof of Concept
1. Start a chain, freeze/vote for an SR in cycle `C1` (well before `ALLOW_NEW_REWARD`/`ALLOW_TVM_VOTE` proposals pass).
2. Let several maintenance cycles elapse so the SR accumulates substantial rewards.
3. Pass `ALLOW_NEW_REWARD` (and let `RewardViCalService` finish backfilling `RewardViStore`) and `ALLOW_TVM_VOTE`, then let more cycles pass so `DelegationStore`'s live Vi for the SR grows large.
4. From a deployed contract, call the TVM `withdrawReward()` native opcode (`Program.withdrawReward()` → `WithdrawRewardProcessor.execute()` → `VoteRewardUtil.withdrawReward()`/`computeReward()`) for the account that voted back in step 1, without ever having withdrawn via the legacy actuator path.
5. Compare the credited `allowance` against what `MortgageService.withdrawReward` (legacy path, called by `VoteWitnessActuator`/`WithdrawBalanceActuator`) would compute for the identical vote/cycle history — the TVM path yields a materially larger amount because `beginVi` resolves to `BigInteger.ZERO` instead of the correct pre-effective-cycle historical Vi baseline.

(Note: I was unable to fully verify, purely from static index review, the exact value/initial-state of `getNewRewardAlgorithmEffectiveCycle()` for every account nor exhaustively trace `Repository.getBeginCycle`/`getEndCycle` initialization for brand-new accounts, since the full contents of `DynamicPropertiesStore.java` were not returned by my tool calls in this session. Confirming the precise numeric magnitude of the discrepancy and any account-state precondition would benefit from a full read of `DynamicPropertiesStore.java` and `Repository`'s vote-cycle bookkeeping in a live Devin session.)

### Citations

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

**File:** chainbase/src/main/java/org/tron/core/service/RewardViCalService.java (L143-153)
```java
  public long getNewRewardAlgorithmReward(long beginCycle, long endCycle,
                                          List<Pair<byte[], Long>> votes) {
    if (!isDone()) {
      logger.warn("rewardViCalService is not done, wait for it");
      try {
        lock.await();
      } catch (InterruptedException e) {
        Thread.currentThread().interrupt();
        throw new TronDBException(e);
      }
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

**File:** chainbase/src/main/java/org/tron/core/store/DelegationStore.java (L120-146)
```java
  public void setWitnessVi(long cycle, byte[] address, BigInteger value) {
    put(buildViKey(cycle, address), new BytesCapsule(value.toByteArray()));
  }

  public BigInteger getWitnessVi(long cycle, byte[] address) {
    BytesCapsule bytesCapsule = get(buildViKey(cycle, address));
    if (bytesCapsule == null) {
      return BigInteger.ZERO;
    } else {
      return new BigInteger(bytesCapsule.getData());
    }
  }

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

**File:** consensus/src/main/java/org/tron/consensus/dpos/MaintenanceManager.java (L94-101)
```java
    DynamicPropertiesStore dynamicPropertiesStore = consensusDelegate.getDynamicPropertiesStore();
    DelegationStore delegationStore = consensusDelegate.getDelegationStore();
    if (dynamicPropertiesStore.useNewRewardAlgorithm()) {
      long curCycle = dynamicPropertiesStore.getCurrentCycleNumber();
      consensusDelegate.getAllWitnesses().forEach(witness -> {
        delegationStore.accumulateWitnessVi(curCycle, witness.createDbKey(), witness.getVoteCount());
      });
    }
```
