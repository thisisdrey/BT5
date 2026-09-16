### Title
Unbounded per-cycle reward computation loop in `MortgageService`/`VoteRewardUtil` can permanently block a voter's withdraw/vote/unfreeze transactions - (File: chainbase/src/main/java/org/tron/core/service/MortgageService.java)

### Summary
The reported issue is a classic "unbounded loop reachable by an ordinary user, causing an out-of-gas/CPU-timeout failure that permanently locks funds" bug class. In java-tron, the analogous unbounded-loop path is the legacy (non-optimized) SR-vote-reward computation, which iterates once per maintenance **cycle** between an account's last-settled cycle and the current cycle, rather than being bounded by a fixed constant like `MAX_VOTE_NUMBER` or `UNFREEZE_MAX_TIMES`.

### Finding Description
`MortgageService.withdrawReward()` / `queryReward()` call `computeReward(beginCycle, endCycle, accountCapsule)`, which for any portion of the cycle range prior to `getNewRewardAlgorithmEffectiveCycle()` falls back to `getOldReward()`: [1](#0-0) 

`getOldReward()` executes a loop whose iteration count equals `end - begin`, i.e., the number of maintenance cycles elapsed since the account last interacted with voting/reward withdrawal, when `allowOldRewardOpt()` is not enabled: [1](#0-0) 

This same unbounded-iteration pattern is duplicated in the TVM-reachable path in `VoteRewardUtil.computeReward` (invoked via `voteWitness`/`withdrawReward` precompiles), and in the `getOldReward` fallback used by `MortgageService.computeReward(beginCycle, endCycle, accountCapsule)`: [2](#0-1) 

Crucially, `MortgageService.withdrawReward()` and `VoteRewardUtil.withdrawReward()` are invoked unconditionally at the very start of every `WithdrawBalanceActuator`, `VoteWitnessActuator`, `UnfreezeBalanceActuator`, and `UnfreezeBalanceV2Actuator` execution: [3](#0-2) [4](#0-3) 

Because `beginCycle`/`endCycle` bookkeeping (`delegationStore.setBeginCycle`/`setEndCycle`) is only advanced *after* a successful `withdrawReward()` call, if the reward computation itself throws (times out / runs out of transaction execution budget) because the cycle gap has grown too large, the account's `beginCycle` is never advanced. Every subsequent attempt to vote, unfreeze, or withdraw balance for that address re-triggers the same (now even larger) unbounded loop, permanently reproducing the failure — mirroring the "Plugin" DoS pattern in the original report where an unbounded iteration blocks all future exits for the affected account.

### Impact Explanation
An account that votes for SRs and then remains inactive (never calls `WithdrawBalanceContract`, `VoteWitnessContract`, `UnfreezeBalanceContract`/`V2`) accumulates an ever-growing `endCycle - beginCycle` gap. When it finally attempts any of these operations, the actuator first invokes `withdrawReward()`, which — under the legacy (`allowOldRewardOpt() == false`) code path — iterates once per elapsed cycle. If the accumulated cycle count is large enough to exceed the transaction's CPU/energy execution budget, the transaction will always fail at this step, and because `beginCycle` is never persisted forward, the account is stuck: it can never successfully vote, unfreeze, or withdraw its balance again. This is a fund-freezing bug affecting an individual account's staked/voted TRX and any allowance, which qualifies as a Medium/High-severity permanent-freezing-of-funds analog.

### Likelihood Explanation
Reachability requires only that a normal user hold votes and skip calling `WithdrawBalanceContract`/`VoteWitnessContract`/`UnfreezeBalanceContract(V2)` for a sufficiently long period (cycles occur roughly every maintenance interval, ~6 hours), and that the network still exercises the legacy fallback path (`allowOldRewardOpt()` disabled, which is a committee-controlled proposal flag rather than a hardcoded, always-on optimization) — see `DynamicPropertiesStore` / `ProposalUtil` toggles referencing `allowOldRewardOpt`. Since this flag is committee-controlled rather than guaranteed on for every account/period, the vulnerable path is plausibly reachable without any privileged action from the caller itself.

### Recommendation
- Always use the O(1)/O(log n) `RewardViCalService.getNewRewardAlgorithmReward` path instead of, or as a fallback bound for, the cycle-by-cycle `getOldReward` loop, regardless of the `allowOldRewardOpt` flag.
- Cap the maximum number of cycles processed per transaction (analogous to `UNFREEZE_MAX_TIMES`/`MAX_VOTE_NUMBER`), and if the account has been inactive longer than that cap, process the reward incrementally across multiple transactions/blocks so `beginCycle` always advances even under partial computation.
- Ensure `beginCycle`/`endCycle` state is updated (or a partial-progress checkpoint saved) even when reward computation must be truncated, so a single expensive transaction never leaves the account in a permanently unresolvable state.

### Proof of Concept
Conceptual PoC (cannot be executed without a running node/testnet):
1. Vote for a witness with `VoteWitnessContract` once, then stop interacting with the account entirely (do not call `WithdrawBalanceContract`, `VoteWitnessContract`, or `UnfreezeBalanceContract`).
2. Let the network run for a very large number of maintenance cycles (e.g., simulate via `payRewardAndDoMaintenance(N)` as used in `framework/src/test/java/org/tron/common/runtime/vm/VoteTest.java`, which already contains a benchmark test, `testRewardAlgorithmBenchmark`, iterating up to 73000 cycles to demonstrate performance degradation of the old algorithm): [5](#0-4) 
3. With `allowOldRewardOpt` disabled, call `WithdrawBalanceContract`/`VoteWitnessContract`/`UnfreezeBalanceContract` for the affected address; the underlying `getOldReward` loop scales linearly with the number of unprocessed cycles and can be driven to exceed available transaction execution budget, after which the account's `beginCycle` never advances and the failure repeats indefinitely on every subsequent attempt.

Note: I was unable to find a hardcoded cap on the number of cycles processed by `getOldReward` or evidence that `allowOldRewardOpt` is unconditionally forced on across all deployments, so exact exploitability in a specific production configuration would need to be validated in a live/testnet environment (recommend a Devin session with node access to confirm actual CPU-time thresholds and current mainnet `allowOldRewardOpt` status).

### Citations

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

**File:** actuator/src/main/java/org/tron/core/actuator/WithdrawBalanceActuator.java (L54-55)
```java
    mortgageService.withdrawReward(withdrawBalanceContract.getOwnerAddress()
        .toByteArray());
```

**File:** actuator/src/main/java/org/tron/core/actuator/VoteWitnessActuator.java (L160-162)
```java
    //
    mortgageService.withdrawReward(ownerAddress);

```

**File:** framework/src/test/java/org/tron/common/runtime/vm/VoteTest.java (L774-824)
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
  }
```
