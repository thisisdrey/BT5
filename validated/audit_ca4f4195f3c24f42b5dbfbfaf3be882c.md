### Title
Uncaught `ArithmeticException` in `MarketSellAssetActuator.execute()` order matching can crash/halt block application - (File: `actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java`)

### Summary
`MarketSellAssetActuator.execute()` calls `matchOrder()` → `matchSingleOrder()` → `MarketUtils.multiplyAndDivide()`, which can throw an unchecked `ArithmeticException` (e.g. `BigInteger` divide-by-zero as a fallback path after the primitive `multiplyExact`/`floorDiv` attempt fails). The `execute()` method's catch clause only handles `ItemNotFoundException | InvalidProtocolBufferException | BalanceInsufficientException | ContractValidateException` — it does **not** catch `ArithmeticException`, unlike sibling actuators (`ExchangeTransactionActuator`, `ExchangeWithdrawActuator`, `ParticipateAssetIssueActuator`, `TransferActuator`) which explicitly catch `ArithmeticException` in their `execute()` methods.

### Finding Description
`execute()` in `MarketSellAssetActuator.java` at lines 152-159 only catches: [1](#0-0) 

while the order-matching path it invokes at lines 140/307-347/402-404 performs division via `MarketUtils.multiplyAndDivide`: [2](#0-1) 

`MarketUtils.multiplyAndDivide` attempts a fast primitive path guarded by `try/catch(ArithmeticException)`, but on overflow it falls back to `BigInteger` arithmetic that is **not** protected from a divide-by-zero divisor `c`: [3](#0-2) 

By contrast, other actuators that perform similar divisions (`ExchangeTransactionActuator`, `ExchangeWithdrawActuator`, `ParticipateAssetIssueActuator`, `TransferActuator`) explicitly catch `ArithmeticException` in `execute()` and convert it into `ContractExeException`, preventing the exception from propagating uncaught during block application: [4](#0-3) [5](#0-4) 

This is analogous to CVE-2022-21351's bug class: an unprivileged, low-privileged client (here, any account broadcasting a `MarketSellAssetContract`) can reach code that is supposed to gracefully fail on bad/edge-case arithmetic but instead can throw an unhandled runtime exception deep inside a consensus-critical execution path (`Manager` applying a block's transactions), which is the "hang or frequently repeatable crash" impact class from the advisory.

### Impact Explanation
If `matchSingleOrder`/`multiplyAndDivide` throws `ArithmeticException` for any resting maker order or taker order combination that isn't caught by `execute()`'s catch clause, the exception propagates as an unchecked `RuntimeException` out of the actuator through `Manager`'s transaction processing during block application. This is executed by every full node applying the block deterministically. An unhandled exception during block/transaction application is a node-crash / consensus-halt class issue (potential DoS across the network since every node applying the same block would hit the same code path), matching the "hang or frequently repeatable crash (complete DOS)" impact described in the CVE.

### Likelihood Explanation
I could not fully confirm within the available tool budget whether `makerBuyQuantity`/`makerSellQuantity` (the divisor `c` passed into `multiplyAndDivide`) can actually reach `0` given the `validate()` checks (`sellTokenQuantity <= 0 || buyTokenQuantity <= 0` rejected at order creation) and the surrounding order-book bookkeeping (`remainCount`/`priceKeysList` cleanup) in `MarketSellAssetActuator.matchOrder` and `MarketUtils`. The reachability of a genuine zero-divisor state (as opposed to only overflow, which is already handled by the `try/catch` inside `multiplyAndDivide` before falling to `BigInteger`) is the key open question. Because of this uncertainty about a concrete zero-divisor trigger, likelihood is assessed as uncertain/medium rather than confirmed-high, and this should be validated further (e.g., with a Devin session that can run the actuator test suite, such as `MarketSellAssetActuatorTest`, and search for edge cases where a maker order's original `buyTokenQuantity`/`sellTokenQuantity` becomes retrievable as 0).

### Recommendation
Add an explicit `catch (ArithmeticException e)` clause to `MarketSellAssetActuator.execute()` (mirroring `ExchangeTransactionActuator`, `ExchangeWithdrawActuator`, `TransferActuator`, and `ParticipateAssetIssueActuator`) that converts the exception into a `ContractExeException` with `ret.setStatus(fee, code.FAILED)`, so a single malformed/edge-case transaction fails gracefully instead of propagating an unchecked exception through block application. Additionally, review `MarketUtils.multiplyAndDivide`'s `BigInteger` fallback to explicitly guard against `c == 0` and throw a checked/expected exception type rather than relying on `BigInteger`'s implicit `ArithmeticException`.

### Proof of Concept
Not established with certainty — I was unable to construct a concrete transaction sequence within the tool budget that drives `makerSellQuantity`/`makerBuyQuantity` (or the analogous divisor in the "taker == maker"/"taker > maker" branches at lines 426-428/459+) to `0` at the point `MarketUtils.multiplyAndDivide` is invoked, given the order-creation validation (`sellTokenQuantity <= 0 || buyTokenQuantity <= 0` rejected) and order-book removal logic. A background Devin session with full repo/test access should attempt to construct such a PoC (e.g., via `MarketSellAssetActuatorTest`/`MarketUtilsTest`) before treating this as confirmed-exploitable; absent that confirmation, this finding should be treated as a plausible but unverified analog.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java (L152-159)
```java
    } catch (ItemNotFoundException
        | InvalidProtocolBufferException
        | BalanceInsufficientException
        | ContractValidateException e) {
      logger.debug(e.getMessage(), e);
      ret.setStatus(fee, code.FAILED);
      throw new ContractExeException(e.getMessage());
    }
```

**File:** actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java (L383-404)
```java
  private void matchSingleOrder(MarketOrderCapsule takerOrderCapsule,
      MarketOrderCapsule makerOrderCapsule, TransactionResultCapsule ret,
      AccountCapsule takerAccountCapsule)
      throws ItemNotFoundException {

    long takerSellRemainQuantity = takerOrderCapsule.getSellTokenQuantityRemain();
    long makerSellQuantity = makerOrderCapsule.getSellTokenQuantity();
    long makerBuyQuantity = makerOrderCapsule.getBuyTokenQuantity();
    long makerSellRemainQuantity = makerOrderCapsule.getSellTokenQuantityRemain();

    // according to the price of maker, calculate the quantity of taker can buy
    // for makerPrice,sellToken is A,buyToken is TRX.
    // for takerPrice,buyToken is A,sellToken is TRX.

    // makerSellTokenQuantity_A/makerBuyTokenQuantity_TRX =
    //   takerBuyTokenQuantityCurrent_A/takerSellTokenQuantityRemain_TRX
    // => takerBuyTokenQuantityCurrent_A = takerSellTokenQuantityRemain_TRX *
    //   makerSellTokenQuantity_A/makerBuyTokenQuantity_TRX

    long takerBuyTokenQuantityRemain = MarketUtils
        .multiplyAndDivide(takerSellRemainQuantity, makerSellQuantity, makerBuyQuantity,
            this.disableJavaLangMath());
```

**File:** chainbase/src/main/java/org/tron/core/capsule/utils/MarketUtils.java (L264-277)
```java
  public static long multiplyAndDivide(long a, long b, long c, boolean disableMath) {
    try {
      long tmp = multiplyExact(a, b, disableMath);
      return floorDiv(tmp, c, disableMath);
    } catch (ArithmeticException ex) {
      // do nothing here, because we will use BigInteger to compute again
    }

    BigInteger aBig = BigInteger.valueOf(a);
    BigInteger bBig = BigInteger.valueOf(b);
    BigInteger cBig = BigInteger.valueOf(c);

    return aBig.multiply(bBig).divide(cBig).longValue();
  }
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L100-105)
```java
    } catch (ItemNotFoundException | InvalidProtocolBufferException
        | ContractValidateException | ArithmeticException e) {
      logger.debug(e.getMessage(), e);
      ret.setStatus(fee, code.FAILED);
      throw new ContractExeException(e.getMessage());
    }
```

**File:** actuator/src/main/java/org/tron/core/actuator/TransferActuator.java (L68-72)
```java
    } catch (BalanceInsufficientException | ArithmeticException | InvalidProtocolBufferException e) {
      logger.debug(e.getMessage(), e);
      ret.setStatus(fee, code.FAILED);
      throw new ContractExeException(e.getMessage());
    }
```
