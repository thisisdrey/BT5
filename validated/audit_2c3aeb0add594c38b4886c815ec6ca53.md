### Title
Witness brokerage fee truncated to zero via double-arithmetic rounding for small transaction fee rewards - (File: chainbase/src/main/java/org/tron/core/service/MortgageService.java)

### Summary
`MortgageService.payReward` computes a witness's brokerage (commission) cut of a block/transaction-fee reward using floating-point multiplication and a truncating cast to `long`. For small `value` amounts (which routinely occur for per-transaction fee rewards paid to the block-producing witness), the computed `brokerageAmount` truncates to `0`, so the witness's configured commission is silently dropped and the entire reward flows to the voter reward pool instead — the same rounding-to-zero fee-loss pattern described in the ArrakisV2 report, just applied to TRON's SR brokerage mechanism rather than an ERC20 manager fee.

### Finding Description
`payReward` is the internal method that splits a reward `value` (paid via `payBlockReward` or `payTransactionFeeReward`) between the witness's own brokerage draw and the voter reward pool: [1](#0-0) 

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

This mirrors exactly the ArrakisV2 `_applyFees` pattern: a percentage (bips-equivalent, here `brokerage/100`) is multiplied by a small integer amount and the fractional remainder is discarded by truncation. `payTransactionFeeReward` is called for every transaction whose fee is credited to the block-producing witness, and `payBlockReward` for every block reward; both ultimately reach `payReward`. Since `value` here is the *per-transaction* fee share credited at the time each transaction is processed (not an aggregated per-cycle total), it is frequently a very small number of sun. For any witness with `brokerage < 100/value`, i.e. whenever `value < 100/brokerage`, the multiplication `brokerageRate * value` rounds below `1.0` and the truncating cast yields `0`, so the witness's brokerage draw for that transaction is entirely lost.

Example: brokerage = 20 (20%), value = 4 sun → `brokerageRate = 0.20`, `brokerageAmount = (long)(0.20 * 4) = (long)0.8 = 0`. The full 4 sun goes to `delegationStore.addReward` (the voter pool) instead of the witness receiving its 20% cut (which should be ~0.8, rounding to 0 or 1 depending on desired rounding, but definitely not silently defaulting to "voter pool gets it all" every single time for any small-value transaction).

### Impact Explanation
Every witness that sets a non-trivial brokerage percentage systematically loses its commission on any transaction whose fee-reward share is small enough to round the product to zero. Because this computation runs per call to `payReward` (i.e., potentially per transaction fee credit as well as per block reward), the truncation is not a rare edge case — it recurs continuously across the network for low-value fee shares, which are common. This causes systematic misallocation of protocol reward funds: value that should accrue to the witness operator instead defaults into the voter reward pool, denying the witness its configured, expected commission indefinitely. This is a Medium-severity fee-accounting defect analogous to the reported ArrakisV2 manager-fee truncation.

### Likelihood Explanation
High likelihood of triggering: this code path executes automatically as part of normal reward distribution (`payBlockReward`/`payTransactionFeeReward`), requiring no attacker action — any witness with even a moderate brokerage rate and any small `value` reward will trigger the truncation on essentially every relevant call. No special privileges or malicious behavior are needed to reach or be affected by this logic; it is an unconditional consequence of standard block-production/reward accounting.

### Recommendation
Replace the double-based percentage computation with exact integer arithmetic (e.g., `BigInteger` or `Math.multiplyExact`/`Math.floorDiv` on `value * brokerage / 100`), consistent with the hardened integer-math patterns already used elsewhere in the codebase (e.g., `ResourceProcessor.calculateGlobalLimitV2` and `MarketUtils.multiplyAndDivide`), to eliminate floating-point rounding-to-zero and ensure the witness brokerage is computed and applied precisely.

### Proof of Concept
1. Witness sets brokerage to any value `B` (1–100) via `UpdateBrokerageContract`/`UpdateBrokerageActuator`.
2. Block production or transaction processing calls `MortgageService.payBlockReward`/`payTransactionFeeReward` with a small `value` (e.g., transaction fee share credited to the witness) such that `value * B < 100`.
3. In `payReward`, `brokerageRate = B/100.0`; `brokerageAmount = (long)(brokerageRate * value)` evaluates to `0`.
4. The entire `value` is credited to `delegationStore.addReward` (voter pool) via `adjustAllowance(witnessAddress, brokerageAmount=0)`; the witness receives none of its configured commission for that reward event, repeatable indefinitely across many small reward credits.

### Citations

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
