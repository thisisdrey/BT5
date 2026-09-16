### Title
Unbounded numeric token-ID length causes `ArrayIndexOutOfBoundsException` in fixed-size buffer copies during market order matching - (File: `chainbase/src/main/java/org/tron/core/capsule/utils/MarketUtils.java`)

### Summary
`MarketUtils` builds several fixed-width keys (`TOKEN_ID_LENGTH` = 19 bytes, sized to hold `Long.MAX_VALUE` as ASCII) by `System.arraycopy`-ing an attacker-supplied `sellTokenId`/`buyTokenId` byte array into a destination buffer whose token-ID slot is hard-coded to `TOKEN_ID_LENGTH` bytes, but the copy length used is `sellTokenId.length`/`buyTokenId.length` — the *source* length — with no check that it is `<= TOKEN_ID_LENGTH`. This mirrors the pjproject CVE-2022-24764 pattern: a fixed-size destination buffer written with an attacker-controlled length taken from the input rather than clamped to the destination capacity. [1](#0-0) [2](#0-1) [3](#0-2) 

### Finding Description
`MarketSellAssetActuator.validate()` only checks that `sellTokenID`/`buyTokenID` are the literal `"_"` (TRX) or pass `TransactionUtil.isNumber(...)`, i.e. that every byte is an ASCII digit. There is no explicit bound on the *length* of the numeric string supplied in the `MarketSellAssetContract`. [4](#0-3) 

Once validated, these attacker-controlled byte arrays flow into `MarketUtils.calculateOrderId`, `MarketUtils.createPairPriceKey` → `doCreatePairPriceKey`, and `MarketUtils.expandTokenIdToPriceArray`, all of which allocate a destination array with a *fixed* `TOKEN_ID_LENGTH` (19-byte) slot per token ID and then copy `sellTokenId.length`/`buyTokenId.length` bytes into that slot:
```java
byte[] result = new byte[TOKEN_ID_LENGTH + TOKEN_ID_LENGTH + ...];
System.arraycopy(sellTokenId, 0, result, 0, sellTokenId.length);
System.arraycopy(buyTokenId, 0, result, TOKEN_ID_LENGTH, buyTokenId.length);
``` [5](#0-4) 

If a caller supplies a numeric token-ID string longer than 19 ASCII digits (e.g. 30+ digits, still passing an "is-all-digits" check), `sellTokenId.length`/`buyTokenId.length` exceeds `TOKEN_ID_LENGTH`, and the second `arraycopy` call writes past the intended slot boundary or throws `ArrayIndexOutOfBoundsException` when the computed destination offset plus length exceeds `result.length`. The same unchecked pattern exists in `expandTokenIdToPriceArray`, used when reading/decoding market price keys. [3](#0-2) 

This call chain is reached from `MarketSellAssetActuator.execute()` during normal block application: `createAndSaveOrder` calls `calculateOrderId`, and `matchOrder`/`saveRemainOrder` call `createPairKey`/`createPairPriceKey` with the same unbounded token IDs. [6](#0-5) [7](#0-6) 

Critically, `execute()`'s catch clause only handles the checked exceptions `ItemNotFoundException | InvalidProtocolBufferException | BalanceInsufficientException | ContractValidateException`; an unchecked `ArrayIndexOutOfBoundsException` raised from the arraycopy calls above is **not caught here** and propagates out of the actuator during transaction/block processing. [8](#0-7) 

### Impact Explanation
Any account can submit a `MarketSellAssetContract` transaction whose `sell_token_id`/`buy_token_id` is an all-digit numeric string longer than 19 characters. Because `TransactionUtil.isNumber` only validates digit composition (not length), such a transaction passes `validate()` and reaches `execute()`, where the fixed-size buffer arithmetic in `MarketUtils` throws an uncaught `ArrayIndexOutOfBoundsException`. Since this exception is not among the checked types caught by the actuator, it propagates up through block/transaction application in `Manager`, which can crash or halt transaction processing on every full node that applies the malicious transaction/block — a consensus-critical, node-crash/halt condition reachable by an unprivileged order placer.

### Likelihood Explanation
The trigger requires only constructing a `MarketSellAssetContract` with an oversized (but all-numeric) token-ID string and broadcasting it as a normal signed transaction — no special privileges, market state, or existing asset are strictly required to reach the vulnerable `calculateOrderId`/`createPairPriceKey` code paths in `execute()`. The `Not support Market Transaction` guard just requires the feature to be enabled by the committee, which is standard on mainnet since Market transactions are a shipped feature. [9](#0-8) 

### Recommendation
Enforce `sellTokenId.length <= MarketUtils.TOKEN_ID_LENGTH` and `buyTokenId.length <= MarketUtils.TOKEN_ID_LENGTH` (or reject numeric strings that don't fit) inside `MarketSellAssetActuator.validate()`, and additionally harden `MarketUtils.doCreatePairPriceKey`, `calculateOrderId`, and `expandTokenIdToPriceArray` to clamp/validate copy lengths against the fixed-size destination buffer before calling `System.arraycopy`, throwing a normal `ContractValidateException`/checked exception instead of allowing an unchecked `ArrayIndexOutOfBoundsException` to propagate out of actuator execution.

### Proof of Concept
1. Craft a `MarketSellAssetContract` with `owner_address` = a funded account, `sell_token_id` = a 25-digit all-numeric ASCII string (e.g. `"1111111111111111111111111"`, all digits so it passes `isNumber`), `buy_token_id` = `"_"` (TRX), and valid `sell_token_quantity`/`buy_token_quantity`.
2. Sign and broadcast the transaction; `validate()` accepts it because `isNumber` doesn't bound length.
3. During `execute()`, `createAndSaveOrder` → `MarketUtils.calculateOrderId` performs `System.arraycopy(sellTokenId, 0, result, addressByteArray.length, sellTokenId.length)` with `sellTokenId.length` (25) exceeding the allotted `TOKEN_ID_LENGTH` (19) slot, throwing `ArrayIndexOutOfBoundsException` that is not caught by the actuator's checked-exception-only catch block, propagating into block/transaction application.

### Citations

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

**File:** chainbase/src/main/java/org/tron/core/capsule/utils/MarketUtils.java (L140-144)
```java
  public static byte[] expandTokenIdToPriceArray(byte[] tokenId) {
    byte[] result = new byte[TOKEN_ID_LENGTH];
    System.arraycopy(tokenId, 0, result, 0, tokenId.length);
    return result;
  }
```

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

**File:** actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java (L181-184)
```java
    if (!dynamicStore.supportAllowMarketTransaction()) {
      throw new ContractValidateException("Not support Market Transaction, need to be opened by"
          + " the committee");
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

**File:** actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java (L309-313)
```java
      throws ItemNotFoundException, ContractValidateException {

    byte[] makerSellTokenID = buyTokenID;
    byte[] makerBuyTokenID = sellTokenID;
    byte[] makerPair = MarketUtils.createPairKey(makerSellTokenID, makerBuyTokenID);
```

**File:** actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java (L575-580)
```java
    byte[] pairPriceKey = MarketUtils.createPairPriceKey(
        sellTokenID,
        buyTokenID,
        sellTokenQuantity,
        buyTokenQuantity
    );
```
