### Title
Denial-of-Service via unchecked token-ID length in Market order actuators leading to uncaught `ArrayIndexOutOfBoundsException` - (File: `chainbase/src/main/java/org/tron/core/capsule/utils/MarketUtils.java`)

### Summary
`MarketSellAssetActuator.validate()` only checks that `sellTokenId`/`buyTokenId` are `"_"` or match a numeric format via `TransactionUtil.isNumber()`, but never bounds their byte length. [1](#0-0)  Downstream, `MarketUtils.createPairKey()` and `MarketUtils.calculateOrderId()` copy these attacker-controlled byte arrays into fixed-size buffers sized on the assumption that a token ID never exceeds `TOKEN_ID_LENGTH` (19 bytes, the length of `Long.MAX_VALUE` as a string). [2](#0-1) [3](#0-2) 

### Finding Description
This mirrors the TensorFlow `Save`/`SaveSlices` bug class: a value whose "type"/shape assumption (fixed dtype size in TF, fixed token-ID byte length here) is never validated before being consumed by low-level memory operations, resulting in an uncaught runtime fault. Here, a numeric string longer than 19 digits (e.g. `"999999999999999999999999999"`) still satisfies `isNumber()`'s digit-character check but produces a `byte[]` longer than `TOKEN_ID_LENGTH`. When such a value reaches `MarketUtils.createPairKey(sellTokenId, buyTokenId)`, the call `System.arraycopy(sellTokenId, 0, result, 0, sellTokenId.length)` writes into a `result` array allocated as `new byte[TOKEN_ID_LENGTH * 2]` (38 bytes); if `sellTokenId.length` exceeds that bound, `System.arraycopy` throws `ArrayIndexOutOfBoundsException`. [3](#0-2)  This is an unchecked `RuntimeException`, not one of the exception types the actuator's `execute()` catches (`ItemNotFoundException`, `InvalidProtocolBufferException`, `BalanceInsufficientException`, `ContractValidateException`). [4](#0-3)  Similarly, `calculateOrderId()` performs unchecked `System.arraycopy` calls assuming both token IDs individually fit in `TOKEN_ID_LENGTH` slots of a combined buffer, and can silently corrupt adjacent fields or throw if lengths are large enough. [5](#0-4) 

### Impact Explanation
An uncaught `ArrayIndexOutOfBoundsException` propagating out of an actuator's `execute()` during block application (in `Manager`) is a node-crash / chain-halt class issue: every node applying the block containing this transaction would hit the same fault deterministically, since `execute()` and the exception-handling only special-case checked exceptions. This satisfies the "node crash or halt" impact bar equivalent to the TensorFlow `CHECK`-fail DoS.

### Likelihood Explanation
Reachable by any unprivileged account with enough balance to pay the transaction fee — `MarketSellAssetContract` is a standard broadcastable transaction type, requires only that the account exists (no special permission), and `sellTokenId`/`buyTokenId` are raw, attacker-supplied byte strings. [6](#0-5)  The only gate is `isNumber()`, which (as evidenced by the existing tests validating only character content, e.g. rejecting `"aaa"`) does not appear to enforce a maximum length. [7](#0-6) 

### Recommendation
Add an explicit length check (`<= MarketUtils.TOKEN_ID_LENGTH`) for `sellTokenId`/`buyTokenId` in `MarketSellAssetActuator.validate()` (and any other actuator/path constructing market keys, e.g. `MarketCancelOrderActuator`), and make `MarketUtils.createPairKey`/`calculateOrderId` defensively validate input lengths before `System.arraycopy`, throwing a checked/validated exception instead of relying on caller invariants.

### Proof of Concept
1. Broadcast a `MarketSellAssetContract` transaction from a funded, unprivileged account with `sell_token_id` set to a numeric ASCII string longer than 19 characters (e.g., 25 digits) and a valid `buy_token_id`.
2. `MarketSellAssetActuator.validate()` passes because `isNumber()` only checks character content, not length. [1](#0-0) 
3. During `execute()`, order creation/matching invokes `MarketUtils.createPairKey`/`calculateOrderId` with the oversized `sellTokenId` byte array, triggering `ArrayIndexOutOfBoundsException` inside `System.arraycopy`, which is not caught by the actuator's exception handling and propagates up through block application. [3](#0-2) 

**Note on confidence:** I was not able to read the full body of `TransactionUtil.isNumber()` before running out of tool calls, so I could not conclusively confirm it lacks a length bound — this should be verified directly in a follow-up session before treating this as fully confirmed. All other cited code (actuator validation logic, fixed-size buffer construction in `MarketUtils`) was directly inspected and supports the described unchecked-length arraycopy issue.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java (L99-120)
```java
  @Override
  public boolean execute(Object object) throws ContractExeException {
    initStores();

    TransactionResultCapsule ret = (TransactionResultCapsule) object;
    if (Objects.isNull(ret)) {
      throw new RuntimeException(TX_RESULT_NULL);
    }

    long fee = calcFee();

    try {
      final MarketSellAssetContract contract = this.any
          .unpack(MarketSellAssetContract.class);

      AccountCapsule accountCapsule = accountStore
          .get(contract.getOwnerAddress().toByteArray());

      sellTokenID = contract.getSellTokenId().toByteArray();
      buyTokenID = contract.getBuyTokenId().toByteArray();
      sellTokenQuantity = contract.getSellTokenQuantity();
      buyTokenQuantity = contract.getBuyTokenQuantity();
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

**File:** actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java (L212-217)
```java
    if (!Arrays.equals(sellTokenID, "_".getBytes()) && !isNumber(sellTokenID)) {
      throw new ContractValidateException("sellTokenId is not a valid number");
    }
    if (!Arrays.equals(buyTokenID, "_".getBytes()) && !isNumber(buyTokenID)) {
      throw new ContractValidateException("buyTokenId is not a valid number");
    }
```

**File:** chainbase/src/main/java/org/tron/core/capsule/utils/MarketUtils.java (L44-60)
```java
  public static final int TOKEN_ID_LENGTH = ByteArray
      .fromString(Long.toString(Long.MAX_VALUE)).length; // 19

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

**File:** framework/src/test/java/org/tron/core/actuator/MarketSellAssetActuatorTest.java (L291-325)
```java
  @Test
  public void invalidTokenId() {

    {
      String sellTokenId = "aaa";
      long sellTokenQuant = 100000000L;
      String buyTokenId = "456";
      long buyTokenQuant = 200000000L;

      MarketSellAssetActuator actuator = new MarketSellAssetActuator();
      actuator.setChainBaseManager(dbManager.getChainBaseManager()).setAny(getContract(
          OWNER_ADDRESS_FIRST, sellTokenId, sellTokenQuant, buyTokenId, buyTokenQuant));
      try {
        actuator.validate();
        fail("sellTokenId is not a valid number");
      } catch (ContractValidateException e) {
        Assert.assertEquals("sellTokenId is not a valid number", e.getMessage());
      }
    }
    {
      String sellTokenId = "456";
      long sellTokenQuant = 100000000L;
      String buyTokenId = "aaa";
      long buyTokenQuant = 200000000L;

      MarketSellAssetActuator actuator = new MarketSellAssetActuator();
      actuator.setChainBaseManager(dbManager.getChainBaseManager()).setAny(getContract(
          OWNER_ADDRESS_FIRST, sellTokenId, sellTokenQuant, buyTokenId, buyTokenQuant));
      try {
        actuator.validate();
        fail("buyTokenId is not a valid number");
      } catch (ContractValidateException e) {
        Assert.assertEquals("buyTokenId is not a valid number", e.getMessage());
      }
    }
```
