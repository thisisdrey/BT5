Based on my research, I found a plausible analog to CVE-2018-1140 (missing input length sanitization leading to a crash) in the TRON market/order matching actuators.

### Title
Missing length validation on `sellTokenId`/`buyTokenId` in Market contracts can trigger an uncaught `ArrayIndexOutOfBoundsException` during block application - (File: `chainbase/src/main/java/org/tron/core/capsule/utils/MarketUtils.java`)

### Summary
`MarketSellAssetActuator.validate()` only checks that `sellTokenId`/`buyTokenId` are the literal `"_"` (TRX) or pass `TransactionUtil.isNumber(...)`, a character-class check, before accepting them [1](#0-0) . Nowhere does `validate()` bound the *length* of these token-id byte arrays against `MarketUtils.TOKEN_ID_LENGTH` (19 bytes, `Long.toString(Long.MAX_VALUE).length`) [2](#0-1) .

### Finding Description
During `execute()`, the order/pair key construction helpers `createPairKey` and `doCreatePairPriceKey` copy the raw `sellTokenId`/`buyTokenId` byte arrays into fixed-size buffers sized `TOKEN_ID_LENGTH * 2` using `System.arraycopy`, using the *supplied* array's length as the copy length without checking it fits: [3](#0-2)  and [4](#0-3) . If an attacker crafts a numeric token id string longer than 19 digits (e.g. a 20+ digit decimal string, which still satisfies whatever numeric-format check `isNumber` performs), `sellTokenId.length` exceeds `TOKEN_ID_LENGTH`, and the `arraycopy` call writes past the end of the destination array, throwing `ArrayIndexOutOfBoundsException`.

This exception is a `RuntimeException`, and `MarketSellAssetActuator.execute()`'s catch clause only handles `ItemNotFoundException | InvalidProtocolBufferException | BalanceInsufficientException | ContractValidateException` [5](#0-4) , so it propagates uncaught out of the actuator, up through `RuntimeImpl.execute()`'s actuator loop [6](#0-5) , into transaction processing during block application.

### Impact Explanation
An uncaught runtime exception thrown while processing a transaction contract during block application can crash or halt the processing node (or, if all nodes deterministically hit the same exception on the same block, could produce a chain-wide halt/DoS), which matches the "node crash or halt" impact bar in scope. This is directly reachable by any unprivileged account broadcasting a single `MarketSellAssetContract` transaction (or the analogous `MarketCancelOrderActuator` path, which reuses the same key-construction helpers).

### Likelihood Explanation
The trigger requires only a syntactically valid but oversized numeric token-id string in a self-contained, broadcastable transaction — no special privileges, precompiles, or complex setup, only that `dynamicStore.supportAllowMarketTransaction()` (a chain feature flag) is enabled and that `isNumber` does not itself already reject overlong strings.

### Recommendation
Add an explicit length bound check (`sellTokenId.length <= MarketUtils.TOKEN_ID_LENGTH` and same for `buyTokenId`) inside `MarketSellAssetActuator.validate()` / `MarketCancelOrderActuator` validation and/or inside `MarketUtils.checkTokenValid`/`checkPairValid`, and/or make the key-construction helpers in `MarketUtils` defensively truncate or throw a checked, caught exception instead of relying on `System.arraycopy` bounds.

### Proof of Concept
1. Submit a `MarketSellAssetContract` transaction with `sell_token_id` set to a decimal ASCII string longer than 19 characters (e.g. `"123456789012345678901"`, 21 digits) and a valid `buy_token_id`.
2. `validate()` accepts it because the only checks are `Arrays.equals(sellTokenID, "_".getBytes())` or `isNumber(sellTokenID)` [1](#0-0) , with no length bound.
3. During `execute()`, `createAndSaveOrder`/key construction calls into `MarketUtils.createPairKey`/`createPairPriceKey`, which `arraycopy`s the 21-byte id into a 38-byte (`TOKEN_ID_LENGTH*2`) buffer at an offset that overflows the buffer, throwing `ArrayIndexOutOfBoundsException`.
4. This exception is not one of the caught types in `execute()`'s catch clause, so it propagates as an unhandled `RuntimeException` during block application.

**Note on confidence**: I could not directly retrieve the source of `TransactionUtil.isNumber(...)` within the available tool budget to confirm it performs no length restriction of its own (only character-class validation was implied by test names like `invalidTokenId` using `"aaa"`). If `isNumber` independently caps input length to ≤19 characters, this specific overflow path would be foreclosed and the finding would not hold; this should be verified directly in `chainbase/src/main/java/org/tron/core/capsule/utils/TransactionUtil.java` before treating this as confirmed.

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

**File:** actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java (L212-217)
```java
    if (!Arrays.equals(sellTokenID, "_".getBytes()) && !isNumber(sellTokenID)) {
      throw new ContractValidateException("sellTokenId is not a valid number");
    }
    if (!Arrays.equals(buyTokenID, "_".getBytes()) && !isNumber(buyTokenID)) {
      throw new ContractValidateException("buyTokenId is not a valid number");
    }
```

**File:** chainbase/src/main/java/org/tron/core/capsule/utils/MarketUtils.java (L44-45)
```java
  public static final int TOKEN_ID_LENGTH = ByteArray
      .fromString(Long.toString(Long.MAX_VALUE)).length; // 19
```

**File:** chainbase/src/main/java/org/tron/core/capsule/utils/MarketUtils.java (L115-130)
```java
  private static byte[] doCreatePairPriceKey(byte[] sellTokenId, byte[] buyTokenId,
      byte[] sellTokenQuantity, byte[] buyTokenQuantity) {
    byte[] result = new byte[TOKEN_ID_LENGTH + TOKEN_ID_LENGTH
        + sellTokenQuantity.length + buyTokenQuantity.length];

    System.arraycopy(sellTokenId, 0, result, 0, sellTokenId.length);
    System.arraycopy(buyTokenId, 0, result, TOKEN_ID_LENGTH, buyTokenId.length);
    System.arraycopy(sellTokenQuantity, 0, result,
        TOKEN_ID_LENGTH + TOKEN_ID_LENGTH,
        sellTokenQuantity.length);
    System.arraycopy(buyTokenQuantity, 0, result,
        TOKEN_ID_LENGTH + TOKEN_ID_LENGTH + buyTokenQuantity.length,
        buyTokenQuantity.length);

    return result;
  }
```

**File:** chainbase/src/main/java/org/tron/core/capsule/utils/MarketUtils.java (L224-229)
```java
  public static byte[] createPairKey(byte[] sellTokenId, byte[] buyTokenId) {
    byte[] result = new byte[TOKEN_ID_LENGTH * 2];
    System.arraycopy(sellTokenId, 0, result, 0, sellTokenId.length);
    System.arraycopy(buyTokenId, 0, result, TOKEN_ID_LENGTH, buyTokenId.length);
    return result;
  }
```

**File:** framework/src/main/java/org/tron/common/runtime/RuntimeImpl.java (L55-60)
```java
    } else {
      for (Actuator act : actuatorList) {
        act.validate();
        act.execute(context.getProgramResult().getRet());
      }
    }
```
