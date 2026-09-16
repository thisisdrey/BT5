### Title
Relative-only precision-tolerance check in `ExchangeWithdrawActuator` reverts legitimate withdrawals whenever the computed counter-token amount is small - ([File: actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java])

### Summary
`ExchangeWithdrawActuator.doValidate()` computes the counterpart token amount (`anotherTokenQuant`) to return to the exchange creator and then verifies the computation is "precise enough" by comparing the rounding remainder to a threshold that is *scaled to* `anotherTokenQuant` itself (`remainder / anotherTokenQuant > 0.0001` in the legacy path, or `remainder.compareTo(anotherTokenQuant * 0.0001)` in the hardened path). This mirrors the Smilee `FinanceIGDelta.deltaHedgeAmount` bug pattern: a tolerance derived purely from the size of a value that can legitimately be small, causing the check to become impossibly strict (and thus revert) whenever that value is small — independent of whether an actual computational error occurred.

### Finding Description
In `doValidate()` [1](#0-0) , once `anotherTokenQuant` is computed via integer/truncating division, the code re-computes a more precise (scale-4) division and takes the fractional remainder, then requires:

```
remainder / anotherTokenQuant > 0.0001  → revert "Not precise enough"
```

or, in the "hardened" branch (`allowHardenExchangeCalculation` proposal parameter, guarded by `AbstractExchangeActuator.allowHarden()` [2](#0-1) ):

```
remainder.compareTo(BigDecimal.valueOf(anotherTokenQuant).multiply(0.0001)) > 0  → revert "Not precise enough"
```

The `remainder` here is essentially the fractional part of the exact rational ratio `secondBalance * tokenQuant / firstBalance` (or the symmetric case), which is uniformly distributed over `[0, 1)` regardless of pool size — it is **not** a floating-point artifact that shrinks as the numbers get smaller. The threshold, however, does shrink proportionally to `anotherTokenQuant`. For any withdrawal where the resulting `anotherTokenQuant` is small (e.g. 1–10000), the required tolerance (`anotherTokenQuant * 0.0001`) becomes a tiny fraction (as low as `0.0001` for `anotherTokenQuant == 1`), while the actual remainder is generically on the order of `0.5`. The probability of passing this check for small `anotherTokenQuant` values is therefore close to zero, causing the withdrawal to revert with `"Not precise enough"` even though there is no real precision error — the balances are all valid, non-zero, and above the `firstTokenBalance == 0 || secondTokenBalance == 0` guard [3](#0-2) .

This is the same bug class as the Smilee `M-8` report: a check meant to tolerate rounding error instead ties its absolute tolerance to the magnitude of a legitimately small quantity, making the check fail (revert) for exactly the small-value cases it should have allowed.

### Impact Explanation
Any account that created an `Exchange` (via `ExchangeCreateContract`, an unprivileged, permissionless action available to any asset issuer/account) and later tries to withdraw a proportionally small amount of liquidity via `ExchangeWithdrawContract` will have their transaction revert with `"Not precise enough"`, even though the exchange pool is perfectly healthy. This denies the exchange creator access to their own locked liquidity for small-quantity withdrawals — a core protocol function (the TRON on-chain Bancor-style exchange feature) becomes unusable for a broad, easily reachable class of inputs, which is a legitimate funds-availability/DoS impact reachable by a single ordinary signed transaction.

### Likelihood Explanation
High for any exchange creator attempting to withdraw a small proportional amount (a very common, unprivileged operation) — no adversarial setup, malicious peer, or special permission is required, only ordinary use of the public `ExchangeWithdrawContract` actuator by the exchange's creator account.

### Recommendation
Do not scale the tolerance purely relative to `anotherTokenQuant`. Instead, bound the check with an absolute floor (e.g., require the remainder to be less than `max(ABSOLUTE_MIN_EPSILON, anotherTokenQuant / 10000)`), or redesign the precision check to validate the round-trip consistency of `tokenQuant`/`anotherTokenQuant` rather than comparing a rounding remainder against a threshold that vanishes for small output amounts.

### Proof of Concept
Given an exchange with `firstTokenBalance = 100_000_000`, `secondTokenBalance = 33_333_333` (so the ratio `secondBalance/firstBalance` is not an exact terminating fraction), a creator submits `ExchangeWithdrawContract` with `tokenId = firstTokenID`, `quant = 3` (i.e., withdrawing 3 units of `firstToken`). Then:
- `anotherTokenQuant = floor(33_333_333 * 3 / 100_000_000) = 0` or `1` depending on rounding, satisfying `anotherTokenQuant > 0`.
- The precise scale-4 division of the same ratio yields a fractional remainder generically far larger than `anotherTokenQuant * 0.0001` (e.g., `0.0001` for `anotherTokenQuant = 1`).
- `doValidate()` throws `ContractValidateException("Not precise enough")` at [4](#0-3) , even though the pool balances are valid and non-zero — reproducing the "reverts due to incorrect check of computation error" pattern from the analog report.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java (L209-212)
```java
    if (firstTokenBalance == 0 || secondTokenBalance == 0) {
      throw new ContractValidateException("Token balance in exchange is equal with 0,"
          + "the exchange has been closed");
    }
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java (L218-243)
```java
    if (Arrays.equals(tokenID, firstTokenID)) {
      anotherTokenQuant = bigSecondTokenBalance.multiply(bigTokenQuant)
          .divideToIntegralValue(bigFirstTokenBalance).longValueExact();
      if (firstTokenBalance < tokenQuant || secondTokenBalance < anotherTokenQuant) {
        throw new ContractValidateException("exchange balance is not enough");
      }

      if (anotherTokenQuant <= 0) {
        throw new ContractValidateException("withdraw another token quant must greater than zero");
      }
      if (allowHarden) {
        BigDecimal remainder = bigSecondTokenBalance.multiply(bigTokenQuant)
            .divide(bigFirstTokenBalance, 4, RoundingMode.HALF_UP)
            .subtract(BigDecimal.valueOf(anotherTokenQuant));
        if (remainder.compareTo(
            BigDecimal.valueOf(anotherTokenQuant).multiply(new BigDecimal("0.0001"))) > 0) {
          throw new ContractValidateException("Not precise enough");
        }
      } else {
        double remainder = bigSecondTokenBalance.multiply(bigTokenQuant)
            .divide(bigFirstTokenBalance, 4, BigDecimal.ROUND_HALF_UP).doubleValue()
            - anotherTokenQuant;
        if (remainder / anotherTokenQuant > 0.0001) {
          throw new ContractValidateException("Not precise enough");
        }
      }
```

**File:** actuator/src/main/java/org/tron/core/actuator/AbstractExchangeActuator.java (L13-15)
```java
  protected boolean allowHarden() {
    return chainBaseManager.getDynamicPropertiesStore().allowHardenExchangeCalculation();
  }
```
