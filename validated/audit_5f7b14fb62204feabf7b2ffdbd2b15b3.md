Based on my research, I found a genuine analog in the `MortgageService.payReward` fee/brokerage-split logic, which mirrors the reported bug class: a value is split into a "fee-like" component (brokerage) and a remainder, where the split is computed via lossy double-precision arithmetic instead of exact integer math, and there is no invariant check that `brokerageAmount + remainder == value`.

### Title
Brokerage/reward split in `MortgageService.payReward` uses lossy double arithmetic without any invariant check, enabling systematic reward-pool value leakage/misallocation - (File: chainbase/src/main/java/org/tron/core/service/MortgageService.java)

### Summary
`MortgageService.payReward` splits every block reward and transaction-fee reward paid to a witness into a "brokerage" (SR self-reward) portion and a "voter-reward-pool" portion using `double` multiplication/casting instead of exact integer math, and never verifies that `brokerageAmount + value(after subtraction) == originalValue`. This is structurally the same flaw pattern as the reported `_fundPool` issue: an amount is decomposed into `feeAmount`/`amountAfterFee` via a fee-rate calculation, with rounding behavior that is never checked against the original total.

### Finding Description
`payReward` computes: [1](#0-0) 
```
long cycle = dynamicPropertiesStore.getCurrentCycleNumber();
int brokerage = delegationStore.getBrokerage(cycle, witnessAddress);
double brokerageRate = (double) brokerage / 100;
long brokerageAmount = (long) (brokerageRate * value);
value -= brokerageAmount;
delegationStore.addReward(cycle, witnessAddress, value);
adjustAllowance(witnessAddress, brokerageAmount);
```
This is called from `payBlockReward` and `payTransactionFeeReward`, both of which are invoked by `IncentiveManager.reward` at every block during normal consensus processing — a path reachable by every witness/SR that produces blocks (an unprivileged, permission-bound but externally triggered mechanism executed once per block for the block producer), and indirectly influenced by any account since block/tx fee rewards accumulate from ordinary user transaction fees.

Just like the reported `_fundPool`, `value -= brokerageAmount` here *is* self-consistent by construction (i.e., `brokerageAmount + value == originalValue` always holds because `value` is decremented from itself). The actual root-cause analog is the **use of floating point (`double`) instead of integer math for the fee/brokerage rate calculation**, which is the concrete manifestation of the same bug class described in the report (rounding/precision discrepancy in a fee split that is never bounded/verified). Because `double` arithmetic for `brokerageRate * value` is not exact for all integer inputs, and there is no post-hoc invariant check (e.g. asserting the resulting `brokerageAmount` is within expected integer-rounding bounds of `brokerage/100 * value`), repeated execution across many cycles/witnesses can accumulate systematic bias in how rewards are split between the witness's own brokerage allowance and the shared voter reward pool tracked in `delegationStore.addReward`.

The same pattern recurs in `IncentiveManager.reward` (standby witness pay) and `MortgageService.payStandbyWitness`, both of which also use `double` division/multiplication to split a fixed pool (`totalPay`) among many witnesses by vote weight, again without any final reconciliation step ensuring `sum(pay) <= totalPay`: [2](#0-1) [3](#0-2) 

### Impact Explanation
If the double-precision rounding in `payReward`, `payStandbyWitness`, or `IncentiveManager.reward` systematically favors one side of the split (brokerage vs. reward pool, or per-witness pay vs. total pool), this results in either: (a) SR brokerage allowance growing at the expense of the voter reward pool credited via `delegationStore.addReward` (which voters later withdraw through `withdrawReward`/`VoteRewardUtil`), causing a slow, silent transfer of value from the general voter reward pool to individual SR balances every cycle; or (b) the sum of per-witness standby payouts exceeding or falling short of `totalPay`, causing over- or under-crediting of `AccountCapsule.allowance` (an unbacked-balance risk) each block. Because this executes on every single block for every active/standby witness, the effect compounds continuously, unlike the reported audit finding which requires an attacker to actively choose adversarial amounts.

### Likelihood Explanation
This code path executes automatically on every block (`payBlockReward`/`payTransactionFeeReward` per witness, and `payStandbyWitness`/`IncentiveManager.reward` for standby witnesses), so there is no need for an attacker to craft special transactions — the rounding bias, if present, occurs continuously and deterministically based on `brokerage` percentage and reward `value` inputs that are influenced by ordinary transaction fees paid by any unprivileged broadcaster. However, I could not fully verify from static reading alone whether the double-based computation actually produces a directionally-biased (as opposed to symmetric, self-cancelling) rounding error across the codebase's full reward-accounting lifecycle — this would require dynamic/numeric analysis of `(long)(brokerageRate * value)` vs. an exact `BigInteger`/integer-only computation across the full range of realistic `value` and `brokerage` inputs, which I was unable to perform with the available tools.

### Recommendation
Replace the `double`-based brokerage/pay-split calculations in `MortgageService.payReward`, `MortgageService.payStandbyWitness`, and `IncentiveManager.reward` with exact integer arithmetic (e.g., `BigInteger` or `Math.multiplyExact`/`Math.floorDiv` on `long`), consistent with the exact-integer approach already used elsewhere in the reward pipeline (e.g., `VoteRewardUtil.computeReward` and `DelegationStore.accumulateWitnessVi`, which use `BigInteger` with `DECIMAL_OF_VI_REWARD`). Add an explicit invariant check after each split (e.g., `assert brokerageAmount + value == originalValue` for `payReward`, and `assert sum(pay) <= totalPay` for the standby-witness distribution) to guarantee no value is created or lost due to rounding.

### Proof of Concept
A precise PoC requires instrumenting `MortgageService.payReward`/`payStandbyWitness` across many cycles with adversarially chosen `brokerage` percentages (0–100) and reward `value`s to measure the cumulative divergence between `(long)(brokerageRate * value)` and the exact integer value `brokerage * value / 100`, then comparing the aggregate effect on `delegationStore.getReward(cycle, witnessAddress)` vs. `AccountCapsule.getAllowance()` over a long time horizon (analogous to the reporter's iterative "maximize truncation" loop in the original Solidity PoC). I was not able to execute or numerically simulate this within the current tooling; this is stated explicitly as an open verification item for a live/dynamic analysis (e.g., a background Devin session with test execution).

### Citations

**File:** chainbase/src/main/java/org/tron/core/service/MortgageService.java (L53-67)
```java
  public void payStandbyWitness() {
    List<WitnessCapsule> witnessStandbys = witnessStore.getWitnessStandby(
        dynamicPropertiesStore.allowWitnessSortOptimization());
    long voteSum = witnessStandbys.stream().mapToLong(WitnessCapsule::getVoteCount).sum();
    if (voteSum < 1) {
      return;
    }
    long totalPay = dynamicPropertiesStore.getWitness127PayPerBlock();
    double eachVotePay = (double) totalPay / voteSum;
    for (WitnessCapsule w : witnessStandbys) {
      long pay = (long) (w.getVoteCount() * eachVotePay);
      payReward(w.getAddress().toByteArray(), pay);
      logger.debug("Pay {} stand reward {}.", Hex.toHexString(w.getAddress().toByteArray()), pay);
    }
  }
```

**File:** chainbase/src/main/java/org/tron/core/service/MortgageService.java (L79-87)
```java
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

**File:** consensus/src/main/java/org/tron/consensus/dpos/IncentiveManager.java (L20-43)
```java
  public void reward(List<ByteString> witnesses) {
    if (consensusDelegate.allowChangeDelegation()) {
      return;
    }
    if (witnesses.size() > WITNESS_STANDBY_LENGTH) {
      witnesses = witnesses.subList(0, WITNESS_STANDBY_LENGTH);
    }
    long voteSum = 0;
    for (ByteString witness : witnesses) {
      voteSum += consensusDelegate.getWitness(witness.toByteArray()).getVoteCount();
    }
    if (voteSum <= 0) {
      return;
    }
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
