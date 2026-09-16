Both `ExchangeInjectContract` and `ExchangeWithdrawContract` confirm the finding: neither carries a `minExpected`/`maxAmount` style slippage-protection field, unlike `ExchangeTransactionContract` which explicitly has `expected` and enforces `anotherTokenQuant < tokenExpected` in `ExchangeWithdrawActuator.doValidate()`/`ExchangeInjectActuator.doValidate()`.

### Title
Missing slippage/minimum-amount protection in ExchangeInject and ExchangeWithdraw actuators enables value extraction via same-block price manipulation - ([File: actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java], [File: actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java])

### Summary
Unlike `ExchangeTransactionContract`, which includes an `expected` field enforced at validation time (`anotherTokenQuant < tokenExpected` throws `ContractValidateException`), the `ExchangeInjectContract` and `ExchangeWithdrawContract` messages carry no counterpart minimum/maximum bound on the computed `anotherTokenQuant`. Both actuators compute the paired-token amount purely from the exchange's current on-chain balances at the time the transaction is processed, with no way for the caller to bound the outcome.

### Finding Description
`ExchangeWithdrawActuator.doValidate()`/`execute()` compute `anotherTokenQuant` proportionally to the live pool ratio (`bigSecondTokenBalance.multiply(bigTokenQuant).divide(bigFirstTokenBalance)`), and `ExchangeInjectActuator` does the same for injection. [1](#0-0)  Neither the protobuf contract (`ExchangeInjectContract`/`ExchangeWithdrawContract`) nor the actuator logic exposes a caller-supplied minimum/maximum bound to check against, in contrast to `ExchangeTransactionContract.expected`, which is explicitly validated: [2](#0-1) . Any transaction (e.g., an `ExchangeTransactionContract` buy/sell) that lands earlier in the same block skews `firstTokenBalance`/`secondTokenBalance` in the `ExchangeCapsule`, changing the ratio used by a subsequently-executed inject/withdraw in that same block, with no on-chain mechanism for the inject/withdraw caller to guard against or abort on an unfavorable outcome. This is the same "no minAmount for slippage control" bug class as the reported Merit Circle `TimeLockPool` issue, applied to java-tron's bonding-curve style `Exchange` (Bancor) contracts.

### Impact Explanation
A block producer or any actor able to order/insert an `ExchangeTransactionContract` ahead of a victim's `ExchangeInjectContract`/`ExchangeWithdrawContract` within the same block can shift the pool ratio so the victim receives materially fewer of the paired token than they would have gotten under the unmanipulated ratio (sandwich-style value extraction), or is forced to inject a disproportionate amount of the paired token. This causes concrete economic loss to unprivileged exchange creators who call inject/withdraw, though it does not create unbacked balances or crash the node.

### Likelihood Explanation
Any account can create an `Exchange` via `ExchangeCreateContract` and thereby become the "creator" permitted to call `ExchangeInjectContract`/`ExchangeWithdrawContract` (both require `accountCapsule.getAddress().equals(exchangeCapsule.getCreatorAddress())`) [3](#0-2) . Anyone can also submit `ExchangeTransactionContract` trades against that exchange to move its balance ratio before the victim's inject/withdraw transaction is packed into the same block, since ordering within a block is controlled by the block-producing SR and not guaranteed to match submission order.

### Recommendation
Add an explicit `min_another_token` (for withdraw) and `max_another_token` (for inject) field to `ExchangeInjectContract`/`ExchangeWithdrawContract`, and enforce it in `doValidate()`/`execute()` analogous to the existing `tokenExpected` check in `ExchangeTransactionActuator`, so callers can bound the amount of paired token they receive or must supply.

### Proof of Concept
1. Attacker (or block producer) has an exchange with balances `firstTokenBalance`, `secondTokenBalance`.
2. Exchange creator submits `ExchangeWithdrawContract` expecting `anotherTokenQuant ≈ secondTokenBalance * tokenQuant / firstTokenBalance` based on the balances they observed.
3. Before the withdraw transaction executes in the block, an `ExchangeTransactionContract` trade (from anyone) executes first, materially changing `firstTokenBalance`/`secondTokenBalance`.
4. `ExchangeWithdrawActuator.execute()` recomputes `anotherTokenQuant` from the now-skewed balances [4](#0-3) , giving the creator a different (potentially much smaller) amount of the paired token than anticipated, with no field available to reject the transaction if the result is below an acceptable threshold.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java (L74-89)
```java
      BigInteger bigFirstTokenBalance = new BigInteger(String.valueOf(firstTokenBalance));
      BigInteger bigSecondTokenBalance = new BigInteger(String.valueOf(secondTokenBalance));
      BigInteger bigTokenQuant = new BigInteger(String.valueOf(tokenQuant));
      if (Arrays.equals(tokenID, firstTokenID)) {
        anotherTokenID = secondTokenID;
        anotherTokenQuant = bigSecondTokenBalance.multiply(bigTokenQuant)
            .divide(bigFirstTokenBalance).longValueExact();
        exchangeCapsule.setBalance(subtractExact(firstTokenBalance, tokenQuant),
            subtractExact(secondTokenBalance, anotherTokenQuant));
      } else {
        anotherTokenID = firstTokenID;
        anotherTokenQuant = bigFirstTokenBalance.multiply(bigTokenQuant)
            .divide(bigSecondTokenBalance).longValueExact();
        exchangeCapsule.setBalance(subtractExact(firstTokenBalance, anotherTokenQuant),
            subtractExact(secondTokenBalance, tokenQuant));
      }
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java (L181-183)
```java
    if (!accountCapsule.getAddress().equals(exchangeCapsule.getCreatorAddress())) {
      throw new ContractValidateException("account[" + readableOwnerAddress + "] is not creator");
    }
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java (L214-227)
```java
    BigDecimal bigFirstTokenBalance = new BigDecimal(String.valueOf(firstTokenBalance));
    BigDecimal bigSecondTokenBalance = new BigDecimal(String.valueOf(secondTokenBalance));
    BigDecimal bigTokenQuant = new BigDecimal(String.valueOf(tokenQuant));
    final boolean allowHarden = allowHarden();
    if (Arrays.equals(tokenID, firstTokenID)) {
      anotherTokenQuant = bigSecondTokenBalance.multiply(bigTokenQuant)
          .divideToIntegralValue(bigFirstTokenBalance).longValueExact();
      if (firstTokenBalance < tokenQuant || secondTokenBalance < anotherTokenQuant) {
        throw new ContractValidateException("exchange balance is not enough");
      }

      if (anotherTokenQuant <= 0) {
        throw new ContractValidateException("withdraw another token quant must greater than zero");
      }
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L217-221)
```java
    long anotherTokenQuant = exchangeCapsule.transaction(tokenID, tokenQuant,
        dynamicStore.allowStrictMath(), allowHarden());
    if (anotherTokenQuant < tokenExpected) {
      throw new ContractValidateException("token required must greater than expected");
    }
```
