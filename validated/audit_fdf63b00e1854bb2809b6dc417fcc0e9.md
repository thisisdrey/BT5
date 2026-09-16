### Title
Witness brokerage fee calculation rounds down to zero for small reward payouts due to double-to-long truncation - (File: chainbase/src/main/java/org/tron/core/service/MortgageService.java)

### Summary
`MortgageService.payReward()` computes a Super Representative's (SR/witness) brokerage commission by converting an integer percentage into a `double` rate and multiplying it by the reward value, then truncating to `long`. When the per-cycle vote reward paid to a witness is small (which routinely happens for low-vote or standby witnesses, or for the fractional per-vote pay computed in `payStandbyWitness`), the truncation causes the computed brokerage amount to round down to `0`, so the witness loses their entitled brokerage commission and the full reward is instead credited to voters.

### Finding Description
`payReward` is the sole place brokerage is deducted from a reward payment: [1](#0-0) 

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

This is exactly the rounding-down fee pattern from the external report: a per-mille/percent style rate is applied to an amount via multiplication and then truncated (here via `(long)` cast, analogous to Solidity's implicit floor division). If `brokerage` is set low (or default `DEFAULT_BROKERAGE = 20`, i.e. 20%) and `value` is small enough that `brokerageRate * value < 1`, `brokerageAmount` becomes `0`, and the SR's brokerage commission opportunity is lost for that cycle's reward, with the full `value` instead flowing to `delegationStore.addReward` (i.e., to the voters).

`payReward` is invoked from two reachable code paths:
- `payBlockReward` / `payTransactionFeeReward`, called during block application when the block-producing witness is paid.
- `payStandbyWitness`, which computes `pay = (long) (w.getVoteCount() * eachVotePay)` — a value that is frequently small for low-vote standby witnesses — and forwards it into `payReward`: [2](#0-1) 

Brokerage itself is settable by any witness account through the `UpdateBrokerageContract`/`UpdateBrokerageActuator`, a broadcastable transaction reachable by any signer who controls a witness account: [3](#0-2) 

A witness can therefore set a brokerage percentage and the truncation behavior in `payReward` will systematically discard the intended commission whenever the per-cycle/per-block reward slice is small, e.g., for standby (127-set) witnesses receiving very small vote-weighted rewards each cycle.

### Impact Explanation
This causes SR/witness operators to be silently deprived of their configured brokerage commission on every reward payment where `brokerageRate * value < 1`. Over many cycles this represents a systematic loss of expected commission revenue for standby or low-vote witnesses, and a corresponding unintended overpayment to voters (funds are not lost from the chain, but are misallocated relative to the on-chain brokerage configuration). This is a Medium-severity fund-accounting/economic-incentive defect consistent with the reported bug class (protocol/party loses its configured fee due to floor-rounding on small amounts).

### Likelihood Explanation
This triggers automatically and deterministically any cycle in which a witness's `brokerage * value / 100 < 1`, i.e., whenever the SR's per-cycle reward slice is small relative to `100 / brokerage`. This is common for standby witnesses (paid via `payStandbyWitness`, which explicitly divides `totalPay` across `voteSum` and can yield tiny per-witness pay), so no attacker action beyond normal witness registration/voting is required — it happens as a natural consequence of the existing reward-distribution mechanics in `MortgageService`, which runs during ordinary block/maintenance-cycle processing.

### Recommendation
Avoid `double` truncation for brokerage/fee-percentage math. Compute `brokerageAmount` using integer arithmetic with a fixed-point numerator, e.g. `brokerageAmount = (value * brokerage) / 100` performed entirely in `long`/`BigInteger`, and additionally consider rounding rather than always flooring, or accumulating remainder/dust so it isn't discarded across cycles (e.g., track fractional remainder and add it back once it reaches 1). At minimum, guard against `brokerage > 0 && value > 0 && brokerageAmount == 0` and decide explicitly whether to round up in favor of the SR or document/accept the dust loss.

### Proof of Concept
1. A witness sets `brokerage = 20` (20%, close to `DelegationStore.DEFAULT_BROKERAGE`) via `UpdateBrokerageContract`.
2. During a maintenance cycle, `payStandbyWitness` computes a small `pay` for a low-vote standby witness, e.g. `pay = 4` (sun).
3. `payReward` computes `brokerageRate = 20.0/100 = 0.2`; `brokerageAmount = (long)(0.2 * 4) = (long)(0.8) = 0`.
4. The witness's brokerage commission for this cycle is `0`; the full `4` sun reward is instead credited via `delegationStore.addReward` for voters to withdraw, even though the witness configured a nonzero brokerage rate. [4](#0-3)

### Citations

**File:** chainbase/src/main/java/org/tron/core/service/MortgageService.java (L53-87)
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
