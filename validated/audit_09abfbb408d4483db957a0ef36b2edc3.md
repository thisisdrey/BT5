### Title
Unbounded per-cycle loop in legacy vote-reward computation allows compute-cost blow-up on ordinary withdraw/vote transactions - ([File: chainbase/src/main/java/org/tron/core/service/MortgageService.java])

### Summary
`MortgageService.computeReward(beginCycle, endCycle, accountCapsule)` falls back to `getOldReward()`, which contains a `for (long cycle = begin; cycle < end; cycle++)` loop that runs once per unclaimed maintenance cycle (nested with a per-vote loop). This is reachable by any account holder simply by not withdrawing/voting for a long time and then issuing an ordinary `WithdrawBalanceContract`, `VoteWitnessContract`, or `UnfreezeBalanceV2Contract` transaction (or calling the TVM `withdrawReward`/`queryReward` opcode path), directly mirroring the reported Isomorph `_updateVirtualPrice()` bug class: a loop whose iteration count grows unboundedly with elapsed time/cycles since last update.

### Finding Description
`MortgageService.computeReward(long beginCycle, long endCycle, AccountCapsule accountCapsule)` [1](#0-0)  splits the reward calculation into an "old" range (before `newRewardAlgorithmEffectiveCycle`) and a "new" Vi-based range. For the old range it calls `getOldReward()`: [2](#0-1) 

```java
private long getOldReward(long begin, long end, List<Pair<byte[], Long>> votes) {
  if (dynamicPropertiesStore.allowOldRewardOpt()) {
    return rewardViCalService.getNewRewardAlgorithmReward(begin, end, votes);
  }
  long reward = 0;
  for (long cycle = begin; cycle < end; cycle++) {
    reward += computeReward(cycle, votes);
  }
  return reward;
}
```

When `allowOldRewardOpt()` is not enabled, the loop runs once per maintenance cycle between `begin` (the account's last-updated cycle) and `end` (`min(currentCycle, newRewardAlgorithmEffectiveCycle)`), and inside each iteration it iterates over every vote the account cast, i.e. `O(cycles * votes)`. The maintenance cycle boundary advances every maintenance interval, driven unconditionally in `MaintenanceManager.doMaintenance()` [3](#0-2) , so `currentCycle - beginCycle` grows without bound the longer an account defers reward withdrawal/re-voting.

This exact concern was already recognized and benchmarked internally: a (disabled) test explicitly measures performance across 73,000 unclaimed cycles and compares the legacy loop-based algorithm to the new O(1) Vi-based algorithm: [4](#0-3) 

The reachable entry points for triggering `computeReward`/`getOldReward` with an unbounded `end - begin` are ordinary, unprivileged transaction types:
- `WithdrawBalanceActuator.execute()` → `mortgageService.withdrawReward(...)` [5](#0-4) 
- `VoteWitnessActuator.countVoteAccount()` → `mortgageService.withdrawReward(ownerAddress)` [6](#0-5) 
- `UnfreezeBalanceV2Actuator.execute()` → `mortgageService.withdrawReward(ownerAddress)` [7](#0-6) 
- HTTP query path `GetRewardServlet.doGet()` → `manager.getMortgageService().queryReward(address)` [8](#0-7) 

Each of these is a normal, unprivileged transaction/API call that any account holder or anonymous API client can trigger.

### Impact Explanation
Because the loop runs during deterministic block execution (inside an actuator invoked from a broadcast transaction), every full node must execute the same `O(cycles*votes)` work to validate/replay the block. An attacker (or simply a long-dormant voter) can accumulate a very large unclaimed cycle gap (years of cycles, as the internal benchmark of 73,000 cycles demonstrates) and then submit a single `WithdrawBalanceContract`/`VoteWitnessContract`/`UnfreezeBalanceV2Contract` transaction that forces disproportionate CPU consumption relative to its declared fee/energy, or causes elongated block-processing latency across the network (all nodes, not just the submitter, must redo the computation while applying the block). If the resulting cost exceeds available processing time/energy budget in a given execution context, this can degrade block-processing throughput/availability — analogous to the reported "unbounded loop / gas exhaustion" issue, but here affecting node liveness/consensus timing rather than a single dApp's function call.

### Likelihood Explanation
Reaching this path requires only: (1) freezing/voting once, (2) allowing many maintenance cycles to pass without withdrawing reward or re-voting, and (3) issuing one ordinary transaction. No special privileges are needed, and the condition (`allowOldRewardOpt()` disabled, i.e., pre-optimization/legacy code path) is a plausible chain state on any deployment that has not yet enabled `allowOldRewardOpt`. The magnitude of the unclaimed-cycle window is entirely attacker/user controlled (bounded only by `newRewardAlgorithmEffectiveCycle`, which network operators set once).

### Recommendation
- Ensure `allowOldRewardOpt()` (or an equivalent O(1)/O(votes) Vi-based computation) is always used for the legacy cycle range instead of the raw `for` loop over cycles, removing the linear-in-cycle-count code path entirely.
- If backward compatibility requires retaining the loop for some transitional period, cap the number of cycles processed per transaction (e.g., force periodic "settlement" so `endCycle - beginCycle` can never exceed a bounded constant) and/or precompute/cache Vi values proactively (as `RewardViCalService` already does) so the on-demand path is always O(votes) regardless of elapsed cycles.
- Add explicit gas/energy or execution-time accounting proportional to `(endCycle - beginCycle) * votes.size()` for the actuators (`WithdrawBalanceActuator`, `VoteWitnessActuator`, `UnfreezeBalanceV2Actuator`) so a pathological unclaimed-cycle count cannot be triggered cheaply.

### Proof of Concept
1. Freeze balance and vote for one or more witnesses from account `A` (via `VoteWitnessContract`), setting `delegationStore.beginCycle` at cycle `N`.
2. Do not withdraw or re-vote for `A`; let the chain proceed through tens of thousands of maintenance cycles (each maintenance cycle triggered automatically by `MaintenanceManager.doMaintenance()` on every maintenance-time boundary).
3. Ensure `dynamicPropertiesStore.allowOldRewardOpt()` remains disabled (default/legacy state) and `newRewardAlgorithmEffectiveCycle` is far in the future or beyond the accumulated gap.
4. Submit a `WithdrawBalanceContract` (or `VoteWitnessContract`, or `UnfreezeBalanceV2Contract`) transaction for account `A`.
5. This invokes `MortgageService.withdrawReward()` → `computeReward(beginCycle, endCycle, accountCapsule)` → `getOldReward()`, executing the `for (long cycle = begin; cycle < end; cycle++)` loop over the entire unclaimed cycle range (demonstrated at 73,000-cycle scale in the existing, disabled `testRewardAlgorithmBenchmark` test) [9](#0-8) , incurring processing cost proportional to the number of unclaimed cycles times the number of votes cast, with no upper bound enforced by the protocol.

### Citations

**File:** chainbase/src/main/java/org/tron/core/service/MortgageService.java (L199-230)
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
    }
    return reward;
  }
```

**File:** chainbase/src/main/java/org/tron/core/service/MortgageService.java (L260-269)
```java
  private long getOldReward(long begin, long end, List<Pair<byte[], Long>> votes) {
    if (dynamicPropertiesStore.allowOldRewardOpt()) {
      return rewardViCalService.getNewRewardAlgorithmReward(begin, end, votes);
    }
    long reward = 0;
    for (long cycle = begin; cycle < end; cycle++) {
      reward += computeReward(cycle, votes);
    }
    return reward;
  }
```

**File:** consensus/src/main/java/org/tron/consensus/dpos/MaintenanceManager.java (L154-162)
```java
    if (dynamicPropertiesStore.allowChangeDelegation()) {
      long nextCycle = dynamicPropertiesStore.getCurrentCycleNumber() + 1;
      dynamicPropertiesStore.saveCurrentCycleNumber(nextCycle);
      consensusDelegate.getAllWitnesses().forEach(witness -> {
        delegationStore.setBrokerage(nextCycle, witness.createDbKey(),
            delegationStore.getBrokerage(witness.createDbKey()));
        delegationStore.setWitnessVote(nextCycle, witness.createDbKey(), witness.getVoteCount());
      });
    }
```

**File:** framework/src/test/java/org/tron/common/runtime/vm/VoteTest.java (L774-823)
```java
  @Ignore
  @Test
  public void testRewardAlgorithmBenchmark() throws Exception {
    byte[] voteContractA = deployContract("VoteA", ABI, CODE);
    byte[] voteContractB = deployContract("VoteB", ABI, CODE);

    // cycle-1
    {
      // freeze balance to get tron power
      freezeBalance(voteContractA);
      freezeBalance(voteContractB);

      // vote through smart contract
      voteWitness(voteContractA,
          Arrays.asList(witnessAStr, witnessBStr),
          Arrays.asList(1234L, 4321L));
      voteWitness(voteContractB,
          Arrays.asList(witnessAStr, witnessBStr),
          Arrays.asList(12L, 21L));

      payRewardAndDoMaintenance(1);
    }

    // cycle-2 ~ cycle-72999
    {
      payRewardAndDoMaintenance(72999);
    }

    // cycle-73000
    {
      int oldTimes = 50;
      long start = System.nanoTime();
      for (int i = 1; i <= oldTimes; i++) {
        if (i % 5 == 0) {
          logger.info("Old-{}: {}", i, (System.nanoTime() - start) / 1000 / i);
        }
        mortgageService.queryReward(voteContractA);
      }
      logger.info("Old-total: {}", (System.nanoTime() - start) / 1000 / oldTimes);

      int newTimes = 10000;
      start = System.nanoTime();
      for (int i = 1; i <= newTimes; i++) {
        if (i % 1000 == 0) {
          logger.info("New-{}: {}", i, (System.nanoTime() - start) / 1000 / i);
        }
        triggerContract(voteContractA, SUCCESS, null, queryRewardBalanceMethod);
      }
      logger.info("New-total: {}", (System.nanoTime() - start) / 1000 / newTimes);
    }
```

**File:** actuator/src/main/java/org/tron/core/actuator/WithdrawBalanceActuator.java (L54-55)
```java
    mortgageService.withdrawReward(withdrawBalanceContract.getOwnerAddress()
        .toByteArray());
```

**File:** actuator/src/main/java/org/tron/core/actuator/VoteWitnessActuator.java (L160-161)
```java
    //
    mortgageService.withdrawReward(ownerAddress);
```

**File:** actuator/src/main/java/org/tron/core/actuator/UnfreezeBalanceV2Actuator.java (L70-72)
```java
    long now = dynamicStore.getLatestBlockHeaderTimestamp();

    mortgageService.withdrawReward(ownerAddress);
```

**File:** framework/src/main/java/org/tron/core/services/http/GetRewardServlet.java (L20-26)
```java
  protected void doGet(HttpServletRequest request, HttpServletResponse response) {
    try {
      long value = 0;
      byte[] address = Util.getAddress(request);
      if (address != null) {
        value = manager.getMortgageService().queryReward(address);
      }
```
