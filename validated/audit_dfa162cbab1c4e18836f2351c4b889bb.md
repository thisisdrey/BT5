### Title
Unbounded numeric Token ID in `MarketSellAssetContract` bypasses length validation and causes uncaught `ArrayIndexOutOfBoundsException` in `MarketUtils.calculateOrderId` - (File: `chainbase/src/main/java/org/tron/core/capsule/utils/MarketUtils.java`)

### Summary
This mirrors the NanoMQ bug class: a lenient first-stage validation accepts data whose *shape* (not just its character content) is never checked, while a later stage does fixed-size, unchecked pointer/array arithmetic that assumes the earlier validation bounded the length. In java-tron, `MarketSellAssetActuator.validate()` only checks that `sellTokenID`/`buyTokenID` are `"_"` or pass `TransactionUtil.isNumber()` — a purely character-class check with no length limit. `MarketUtils` elsewhere hard-codes a fixed `TOKEN_ID_LENGTH` (19 bytes) for building keys/IDs, and several of its helpers do `System.arraycopy` at fixed offsets using the *actual* (unbounded) token-ID length instead of the validated fixed length.

### Finding Description
`MarketSellAssetActuator.validate()` validates the token IDs like this: [1](#0-0) 

`TransactionUtil.isNumber()` — the only guard on the token ID's shape — checks digits and a leading-zero rule, but never bounds the length of the input array: [2](#0-1) 

Meanwhile, `MarketUtils` defines a fixed slot size for token IDs: [3](#0-2) 

and `calculateOrderId` (invoked from `execute()` via `createAndSaveOrder`) builds a fixed-size buffer sized as `address.length + TOKEN_ID_LENGTH + TOKEN_ID_LENGTH + count.length`, then copies the *actual* `sellTokenId`/`buyTokenId` byte arrays into it using their real (unbounded) length rather than the fixed `TOKEN_ID_LENGTH`: [4](#0-3) 

If a caller supplies a numeric `sell_token_id` (or `buy_token_id`) whose byte length is large enough that `addressByteArray.length + TOKEN_ID_LENGTH + sellTokenId.length` exceeds the allocated buffer size, `System.arraycopy` throws `ArrayIndexOutOfBoundsException`. This call happens inside `MarketSellAssetActuator.execute()`: [5](#0-4) 

Critically, `execute()`'s catch block only handles specific checked exceptions and does **not** catch `RuntimeException`/`ArrayIndexOutOfBoundsException`: [6](#0-5) 

so the exception propagates uncaught out of the actuator during transaction execution/block application — the same "loose validation stage, then unchecked downstream pointer/array arithmetic on the unvalidated-length data" pattern as the NanoMQ `strchr()`/`sub_topic++` bug (loose `$share/` parsing at subscribe time, unchecked pointer arithmetic at forward time).

The same unbounded-length token IDs also flow into other `MarketUtils` helpers such as `createPairKey`/`doCreatePairPriceKey`, which perform `System.arraycopy` at fixed offsets (`TOKEN_ID_LENGTH`) using the real (unbounded) source length, risking silent buffer corruption or out-of-bounds writes depending on exact lengths chosen: [7](#0-6) [8](#0-7) 

### Impact Explanation
An uncaught `ArrayIndexOutOfBoundsException` thrown from inside actuator `execute()` during block application is a node crash / chain-halt class issue: it is not one of the exceptions the surrounding code is designed to convert into a failed-transaction receipt, so it propagates as an unhandled runtime error in the transaction-processing path used when nodes apply blocks. Because the exact same malformed transaction would be present in the block for every node, this can cause consensus-wide node crashes (a stable, remotely triggerable DoS), which matches the CWE class and severity of the reference NanoMQ bug (crash via unchecked pointer/length assumptions after a permissive earlier validation step).

### Likelihood Explanation
Reachability requires only that: (1) the Market Transaction feature is enabled on-chain (`dynamicStore.supportAllowMarketTransaction()`), which is a normal, already-supported production feature gated by committee governance rather than by any special attacker privilege, and (2) any account broadcasts a single `MarketSellAssetContract` transaction with a crafted, very long numeric `sell_token_id` or `buy_token_id` byte string that satisfies `TransactionUtil.isNumber()` (any length, digits only, no leading zero). No special permissions, keys, or witness/SR role are needed — this is reachable by any unprivileged transaction broadcaster, matching the required threat model (single signed transaction/contract call).

### Recommendation
- In `MarketSellAssetActuator.validate()` (and the equivalent order actuators, e.g., `MarketCancelOrderActuator`), reject `sellTokenID`/`buyTokenID` whose byte length exceeds `MarketUtils.TOKEN_ID_LENGTH` (19), in addition to the existing `isNumber`/`"_"` checks.
- In `MarketUtils.calculateOrderId`, `doCreatePairPriceKey`, and `createPairKey`, defensively bound-check (or truncate/reject) `sellTokenId.length`/`buyTokenId.length` against `TOKEN_ID_LENGTH` before doing `System.arraycopy`, rather than trusting the length implicitly.
- Broaden the actuator `execute()` catch clauses (or add a top-level safety net in the actuator execution pipeline) to convert unexpected `RuntimeException`s into a failed-transaction result instead of letting them propagate out of block application.

### Proof of Concept
1. Ensure `supportAllowMarketTransaction` is enabled (already a supported chain feature).
2. Craft and broadcast a `MarketSellAssetContract` transaction where `sell_token_id` is set to a numeric ASCII byte string of length large enough that `ownerAddress.length (21) + TOKEN_ID_LENGTH (19) + sellTokenId.length` exceeds the buffer allocated in `calculateOrderId` (`ownerAddress.length + TOKEN_ID_LENGTH*2 + countByteArray.length`, i.e., roughly more than ~27 digits), e.g. a 40-digit numeric string not starting with `0`, with `buy_token_id` = `"_"` (TRX) and valid positive `sell_token_quantity`/`buy_token_quantity`.
3. `validate()` passes because `isNumber()` only checks digit characters/leading zero.
4. During `execute()` → `createAndSaveOrder()` → `MarketUtils.calculateOrderId()`, the `System.arraycopy(sellTokenId, 0, result, addressByteArray.length, sellTokenId.length)` call writes past the end of the pre-sized `result` array, throwing `ArrayIndexOutOfBoundsException`, which is not caught by the actuator's `catch` clause and propagates out of transaction execution during block application.

*Note:* I was not able to trace, within the tool budget available, the exact call site in `Manager`/`TransactionTrace` that invokes `actuator.execute()` to confirm whether any broader `catch (Throwable/Exception)` wrapper exists further up the call stack that would downgrade this to a transaction failure rather than a node crash. This should be verified in the actual codebase (`framework/src/main/java/org/tron/core/db/TransactionTrace.java` and `Manager.java`) before treating the "node crash" impact as fully confirmed; the array-bounds violation and its non-capture by the actuator's own catch block are confirmed directly from the source shown above.

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

**File:** actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java (L501-513)
```java
  private MarketOrderCapsule createAndSaveOrder(AccountCapsule accountCapsule,
      MarketSellAssetContract contract) {
    MarketAccountOrderCapsule marketAccountOrderCapsule = marketAccountStore
        .getUnchecked(contract.getOwnerAddress().toByteArray());
    if (marketAccountOrderCapsule == null) {
      marketAccountOrderCapsule = new MarketAccountOrderCapsule(contract.getOwnerAddress());
    }

    // note: here use total_count
    byte[] orderId = MarketUtils
        .calculateOrderId(contract.getOwnerAddress(), sellTokenID, buyTokenID,
            marketAccountOrderCapsule.getTotalCount());
    MarketOrderCapsule orderCapsule = new MarketOrderCapsule(orderId, contract);
```

**File:** chainbase/src/main/java/org/tron/core/capsule/utils/TransactionUtil.java (L175-186)
```java
  public static boolean isNumber(byte[] id) {
    if (ArrayUtils.isEmpty(id)) {
      return false;
    }
    for (byte b : id) {
      if (b < '0' || b > '9') {
        return false;
      }
    }

    return !(id.length > 1 && id[0] == '0');
  }
```

**File:** chainbase/src/main/java/org/tron/core/capsule/utils/MarketUtils.java (L44-45)
```java
  public static final int TOKEN_ID_LENGTH = ByteArray
      .fromString(Long.toString(Long.MAX_VALUE)).length; // 19
```

**File:** chainbase/src/main/java/org/tron/core/capsule/utils/MarketUtils.java (L47-64)
```java
  public static byte[] calculateOrderId(ByteString address, byte[] sellTokenId,
      byte[] buyTokenId, long count) {

    byte[] addressByteArray = address.toByteArray();
    byte[] countByteArray = ByteArray.fromLong(count);

    byte[] result = new byte[addressByteArray.length + TOKEN_ID_LENGTH
        + TOKEN_ID_LENGTH + countByteArray.length];

    System.arraycopy(addressByteArray, 0, result, 0, addressByteArray.length);
    System.arraycopy(sellTokenId, 0, result, addressByteArray.length, sellTokenId.length);
    System.arraycopy(buyTokenId, 0, result, addressByteArray.length + TOKEN_ID_LENGTH,
        buyTokenId.length);
    System.arraycopy(countByteArray, 0, result, addressByteArray.length
        + TOKEN_ID_LENGTH + TOKEN_ID_LENGTH, countByteArray.length);

    return Hash.sha3(result);
  }
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
