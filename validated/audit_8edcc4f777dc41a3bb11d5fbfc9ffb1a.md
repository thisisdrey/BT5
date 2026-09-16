### Title
Brokerage (protocol/SR fee) commission is silently rounded to zero on low-value reward payments due to double-precision truncation - ([File: chainbase/src/main/java/org/tron/core/service/MortgageService.java])

### Summary
`MortgageService.payReward()` computes a Super Representative's brokerage cut using floating-point (`double`) math and then truncates the result to a `long`. When the payment `value` passed into `payReward()` is small relative to the configured `brokerage` percentage, `brokerageAmount` rounds down to `0`, and the SR permanently loses its commission for that payment while the full `value` is credited to voter rewards. This mirrors the Sherlock M-18 pattern: a percentage-based protocol/operator fee computed via fixed/floating-point division on a small principal rounds to zero, and because there is no "carry-over" of the lost fractional fee, the loss is unrecoverable and can be repeated indefinitely.

### Finding Description
`payReward()` is the single choke point through which all witness/SR income (standby-witness pay, block rewards, and transaction-fee rewards) passes: [1](#0-0) 

```java
public void payBlockReward(byte[] witnessAddress, long value) { ... payReward(witnessAddress, value); }
public void payTransactionFeeReward(byte[] witnessAddress, long value) { ... payReward(witnessAddress, value); }

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

`brokerage` is an SR-controlled percentage (0–100) set via `UpdateBrokerageActuator`/`UpdateBrokerageContract`, which any account can set for their own witness node: [2](#0-1) 

For any `value` such that `brokerage * value / 100 < 1` (e.g. `brokerage = 1` and `value ≤ 99`, or `brokerage = 20` (the `DEFAULT_BROKERAGE`) and `value ≤ 4`), `brokerageAmount` truncates to `0` and the entire `value` is credited to the voter reward pool instead of the SR's allowance — the SR's commission for that specific payment is permanently lost. There is no accrual mechanism (unlike a running remainder) to recover this fractional loss on the next call, exactly the structural flaw identified in the referenced report (`pool.lastUpdated`/checkpoint pattern causing loss to be un-recoverable).

`payTransactionFeeReward` is fed by per-block transaction fee accounting driven ultimately by ordinary user transactions (transaction fees are attacker/user-controlled inputs), and `payStandbyWitness`'s per-witness `pay` value is derived from `(long) (w.getVoteCount() * eachVotePay)`, which can also be arbitrarily small for witnesses with low relative vote counts: [3](#0-2) 

Because Manager.java calls `payTransactionFeeReward`/`payBlockReward` per block during block application, and transaction fees collected per block are influenced by the volume/size of transactions any unprivileged broadcaster submits, an attacker (or just organic low-fee-transaction traffic) can keep the per-call `value` small enough, block after block, to repeatedly zero out the brokerage cut for a targeted low-brokerage SR.

### Impact Explanation
This causes a protocol-level (SR commission) fund-accounting error: the SR's earned brokerage is silently and permanently lost on any reward payment small enough to round to zero, with no compensating mechanism. Compounded over many blocks/cycles, this represents a sustained, unrecoverable loss of protocol/SR fee income — analogous to the Medium-severity M-18 finding, where the fee-taking side (lender/protocol) is deprived of its due cut because of truncation, while the other side (borrower/voter) benefits by paying/receiving the full untaxed amount.

### Likelihood Explanation
Likelihood is moderate: it requires either (a) an SR intentionally or incidentally setting a low `brokerage` value, or (b) block-level `value` (transaction-fee share, block reward share, or standby pay share) being small — both plausible in low-fee/low-activity periods or for SRs with low vote share, and reachable purely through normal block processing without any privileged action. It does not require compromising any account, only ordinary transaction activity and witness parameter configuration that any account already controls via `UpdateBrokerageContract`.

### Recommendation
Compute `brokerageAmount` using integer arithmetic that preserves precision (e.g., `BigInteger`/`Math.multiplyExact` scaled math, `value * brokerage / 100` with intermediate widening, or carrying forward a remainder across calls) rather than `(long) ((double) brokerage / 100 * value)`, so fractional brokerage that would otherwise round to zero accumulates instead of being discarded. This mirrors the "hardened" `BigInteger`-based patterns already used elsewhere in the codebase (e.g., `RepositoryImpl.calculateGlobalEnergyLimit`, `ExchangeWithdrawActuator`'s hardened precision checks) and should be applied consistently to `MortgageService.payReward`, `payStandbyWitness`, and `MortgageService.computeReward` (which uses a similar `double voteRate * totalReward` pattern for the legacy reward algorithm).

### Proof of Concept
1. As any account, register/own a witness and call `UpdateBrokerageContract` to set `brokerage = 1` (1%), a valid value per `UpdateBrokerageActuator.validate()` (`0 ≤ brokerage ≤ 100`).
2. Ensure the per-block `value` passed to `payTransactionFeeReward`/`payBlockReward`/`payStandbyWitness` for this witness stays below `100` (e.g., low block activity, or a witness with low relative vote share receiving a small slice of `Witness127PayPerBlock`).
3. Observe in `MortgageService.payReward`: `brokerageAmount = (long) (0.01 * value)` truncates to `0` whenever `value < 100`, so `delegationStore.addReward` credits the full `value` to voter rewards and `adjustAllowance` credits `0` to the SR — the SR's brokerage is lost for that payment, repeatable every such block/cycle.

*Note: I could not fully inspect `framework/src/main/java/org/tron/core/db/Manager.java` (the file appeared truncated/empty in the tool output) to confirm the exact per-block `value` computation and how transaction-fee volume maps to `payTransactionFeeReward` calls. This should be verified directly in that file to fully confirm the degree to which an unprivileged transaction sender can control the magnitude of `value` per call.*

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

**File:** chainbase/src/main/java/org/tron/core/service/MortgageService.java (L69-87)
```java
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

**File:** actuator/src/main/java/org/tron/core/actuator/UpdateBrokerageActuator.java (L49-56)
```java
    byte[] ownerAddress = updateBrokerageContract.getOwnerAddress().toByteArray();
    int brokerage = updateBrokerageContract.getBrokerage();

    delegationStore.setBrokerage(ownerAddress, brokerage);
    ret.setStatus(fee, code.SUCESS);

    return true;
  }
```
