### Title
Missing Slippage Protection in `ExchangeInjectActuator` and `ExchangeWithdrawActuator` Enables Front-Running of Bancor-Style Exchange Pools - (File: `actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java`, `actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java`)

### Summary
The TRON on-chain Bancor-style exchange (`ExchangeCapsule`) supports three permissionless operations: swap (`ExchangeTransactionContract`), inject liquidity (`ExchangeInjectContract`) and withdraw liquidity (`ExchangeWithdrawContract`). Only the swap operation carries a user-supplied minimum-output field (`expected`) that is checked against the value computed at execution time. `ExchangeInjectActuator` and `ExchangeWithdrawActuator` compute the counterpart token amount (`anotherTokenQuant`) purely from the exchange's *current* `firstTokenBalance`/`secondTokenBalance` ratio at execution time, with no user-supplied bound to protect against ratio movement between signing and execution. This is the same bug class as the reported `BaseCustomAccounting` issue: liquidity-modifying calls lack slippage protection and can be sandwiched by a swap that shifts the pool ratio, causing the liquidity provider to inject more value than intended or withdraw less value than intended.

### Finding Description
`ExchangeTransactionActuator` protects the taker with an explicit `expected` field checked in `doValidate()`: [1](#0-0) 

In contrast, `ExchangeInjectActuator.execute()` recomputes `anotherTokenQuant` from the live pool balances with no user-controlled bound: [2](#0-1) 

The same pattern with no user-specified min/max recurs in `doValidate()`, where `anotherTokenQuant` is derived solely from `firstTokenBalance`/`secondTokenBalance` read at validation time, with checks limited to balance sufficiency and balance-limit ceilings, not to any expectation set by the caller: [3](#0-2) 

`ExchangeWithdrawActuator` has the identical structural gap. `execute()` recomputes `anotherTokenQuant` from the current pool ratio: [4](#0-3) 

`doValidate()` only performs a rounding/precision sanity check (comparing the freshly-computed ratio against itself, not against a user intent) — it is not a slippage check because both sides of the comparison are derived from the same live, potentially manipulated state: [5](#0-4) 

Because `ExchangeInjectContract` and `ExchangeWithdrawContract` protobuf messages only carry `tokenId`/`quant` (no `expected`/minimum field, unlike `ExchangeTransactionContract`), a liquidity provider has no way to bound the counterpart amount they will pay or receive. Any unprivileged account can broadcast an `ExchangeTransactionContract` (a swap) against the same exchange to shift `firstTokenBalance`/`secondTokenBalance` before the victim's inject/withdraw transaction is packed into the same or next block, since transaction ordering within a block is controlled by the block producer and is not guaranteed to match submission order. This directly mirrors the report's root cause: liquidity-modifying operations can be front-run by swaps that move the pool's price, and the slippage check (where one exists) is insufficient or entirely absent.

### Impact Explanation
- For `ExchangeInjectActuator`: an attacker can front-run the victim's inject transaction with a swap that inflates the required `anotherTokenQuant`, causing the victim to pay far more of the counterpart asset than intended for the same declared `tokenQuant`, resulting in direct fund loss to the victim (value effectively captured by the attacker via the manipulated pool state and their own follow-up swap).
- For `ExchangeWithdrawActuator`: an attacker can front-run the exchange creator's withdrawal with a swap that deflates the ratio, causing the creator to receive less `anotherTokenQuant` than the pool state implied when they signed the transaction, again resulting in fund loss.
- Both cases match the "Medium/High" bar of unauthorized value extraction / theft of funds for a permissionless, signature-only-gated operation, consistent with the original UniswapV4 hooks finding.

### Likelihood Explanation
Both `ExchangeInjectContract` and `ExchangeWithdrawContract` are broadcastable by any signed transaction from the exchange creator (withdraw/inject require being the exchange creator, but the front-running swap `ExchangeTransactionContract` is fully permissionless and can be sent by anyone). Sandwiching a target transaction via mempool observation and fee/ordering manipulation is a standard, low-cost technique, and no special privileges are required to execute the attack — only knowledge of the target transaction and a swap against the exchange.

### Recommendation
Add a `expected`/minimum (and optionally maximum) field to `ExchangeInjectContract` and `ExchangeWithdrawContract`, and validate the freshly computed `anotherTokenQuant` against that user-supplied bound in `doValidate()`/`execute()` of `ExchangeInjectActuator` and `ExchangeWithdrawActuator`, mirroring the existing `expected` check in `ExchangeTransactionActuator`.

### Proof of Concept
1. Attacker observes a pending `ExchangeInjectContract` from account V injecting `tokenQuant` of `firstTokenId` into exchange `E`.
2. Attacker broadcasts an `ExchangeTransactionContract` swap against `E` (buying/selling `firstTokenId`) with higher fee/priority so it lands first in the block, shifting `firstTokenBalance`/`secondTokenBalance`.
3. V's `ExchangeInjectContract` executes next; `ExchangeInjectActuator.execute()` recomputes `anotherTokenQuant` at `actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java:73-82` using the now-manipulated ratio, deducting a larger amount of `anotherTokenId` from V's balance than V expected when signing.
4. Attacker reverses their swap in a subsequent transaction, restoring the pool ratio and pocketing the difference extracted from V, with no on-chain check (`expected`/minimum field) available to have prevented step 3.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L217-221)
```java
    long anotherTokenQuant = exchangeCapsule.transaction(tokenID, tokenQuant,
        dynamicStore.allowStrictMath(), allowHarden());
    if (anotherTokenQuant < tokenExpected) {
      throw new ContractValidateException("token required must greater than expected");
    }
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java (L71-83)
```java
      if (Arrays.equals(tokenID, firstTokenID)) {
        anotherTokenID = secondTokenID;
        anotherTokenQuant = floorDiv(multiplyExact(
            secondTokenBalance, tokenQuant), firstTokenBalance);
        exchangeCapsule.setBalance(addExact(firstTokenBalance, tokenQuant),
            addExact(secondTokenBalance, anotherTokenQuant));
      } else {
        anotherTokenID = firstTokenID;
        anotherTokenQuant = floorDiv(multiplyExact(
            firstTokenBalance, tokenQuant), secondTokenBalance);
        exchangeCapsule.setBalance(addExact(firstTokenBalance, anotherTokenQuant),
            addExact(secondTokenBalance, tokenQuant));
      }
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java (L215-231)
```java
    if (Arrays.equals(tokenID, firstTokenID)) {
      anotherTokenID = secondTokenID;
      anotherTokenQuant = bigSecondTokenBalance.multiply(bigTokenQuant)
          .divide(bigFirstTokenBalance).longValueExact();
      newTokenBalance = addExact(firstTokenBalance, tokenQuant);
      newAnotherTokenBalance = addExact(secondTokenBalance, anotherTokenQuant);
    } else {
      anotherTokenID = firstTokenID;
      anotherTokenQuant = bigFirstTokenBalance.multiply(bigTokenQuant)
          .divide(bigSecondTokenBalance).longValueExact();
      newTokenBalance = addExact(secondTokenBalance, tokenQuant);
      newAnotherTokenBalance = addExact(firstTokenBalance, anotherTokenQuant);
    }

    if (anotherTokenQuant <= 0) {
      throw new ContractValidateException("the calculated token quant  must be greater than 0");
    }
```

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

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java (L228-243)
```java
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
