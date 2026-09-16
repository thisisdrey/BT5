### Title
Unchecked-null protobuf parse in `MarketPairPriceToOrderStore.get()` causes uncaught `NullPointerException` during market order execution, crashing block application - (File: `chainbase/src/main/java/org/tron/core/store/MarketPairPriceToOrderStore.java`)

### Summary
`MarketPairPriceToOrderStore.get(byte[] key)` overrides the base `ITronChainBase` contract and, unlike a normal checked lookup, never verifies whether the requested key actually exists before constructing a `MarketOrderIdListCapsule`. If the key is absent, `revokingDB.get(key)` returns `null`, and that `null` is fed straight into `new MarketOrderIdListCapsule(value)`, which calls `MarketOrderIdList.parseFrom(null)`. Protobuf's `parseFrom` throws an unchecked `NullPointerException` on a null byte array, which is not caught anywhere in the call chain (the actuators only catch `ItemNotFoundException`, `InvalidProtocolBufferException`, `BalanceInsufficientException`). This mirrors the CVE-2018-19407 pattern: an object that should have been initialized/validated first is instead dereferenced while still uninitialized (`null`), producing a NULL-pointer crash reachable from ordinary caller-controlled input.

### Finding Description [1](#0-0) 

```java
@Override
public MarketOrderIdListCapsule get(byte[] key) throws ItemNotFoundException {
  byte[] value = revokingDB.get(key);
  return new MarketOrderIdListCapsule(value);
}
```

Unlike the generic pattern used elsewhere (`getUnchecked` returning `null` explicitly, or throwing `ItemNotFoundException` when absent), this override neither checks `value == null` nor throws `ItemNotFoundException` — it silently passes a possibly-null byte array into the capsule constructor: [2](#0-1) 

```java
public MarketOrderIdListCapsule(final byte[] data) {
  try {
    this.orderIdList = MarketOrderIdList.parseFrom(data);
  } catch (InvalidProtocolBufferException e) {
    logger.debug(e.getMessage(), e);
  }
}
```

`MarketOrderIdList.parseFrom(null)` throws `NullPointerException` (a `RuntimeException`, not `InvalidProtocolBufferException`), so the `catch` clause does not intercept it — the exception propagates straight out of the actuator.

This `get()` is directly invoked from the actuators that process user-broadcastable market contracts, with no prior existence check on the exact key used: [3](#0-2) 

```java
byte[] pairPriceKey = MarketUtils.createPairPriceKey(
    orderCapsule.getSellTokenId(),
    orderCapsule.getBuyTokenId(),
    orderCapsule.getSellTokenQuantity(),
    orderCapsule.getBuyTokenQuantity()
);
MarketOrderIdListCapsule orderIdListCapsule = pairPriceToOrderStore.get(pairPriceKey);
// delete order
orderIdListCapsule.removeOrder(orderCapsule, orderStore, pairPriceKey, pairPriceToOrderStore);
```

and similarly in the order matching path: [4](#0-3) 

```java
byte[] pairPriceKey = priceKeysList.get(0);
// if not exists
MarketOrderIdListCapsule orderIdListCapsule = pairPriceToOrderStore.get(pairPriceKey);
```

`MarketCancelOrderActuator.execute()` re-derives `pairPriceKey` from the order's own sell/buy token IDs and quantities at execution time and only wraps `ItemNotFoundException | InvalidProtocolBufferException | BalanceInsufficientException`: [5](#0-4) 

```java
} catch (ItemNotFoundException
    | InvalidProtocolBufferException
    | BalanceInsufficientException e) {
  logger.debug(e.getMessage(), e);
  ret.setStatus(fee, code.FAILED);
  throw new ContractExeException(e.getMessage());
}
```

`validate()` checks that the order itself exists, is active, and belongs to the caller, but never verifies that the corresponding `pairPriceToOrderStore` bucket still exists at execution time: [6](#0-5) 

```java
MarketOrderCapsule marketOrderCapsule;
try {
  marketOrderCapsule = orderStore.get(orderId.toByteArray());
} catch (ItemNotFoundException ex) {
  throw new ContractValidateException("orderId not exists");
}
if (!marketOrderCapsule.isActive()) {
  throw new ContractValidateException("Order is not active!");
}
if (!marketOrderCapsule.getOwnerAddress().equals(ownerAccount.getAddress())) {
  throw new ContractValidateException("Order does not belong to the account!");
}
```

If for any reason the pair-price bucket has already been removed (e.g., concurrent matching by another transaction in the same block consumed and deleted the same price bucket that this cancel order still points to, before this actuator executes — a TOCTOU window between `validate()` and `execute()` inherent to sequential-in-block actuator execution, or any state divergence between `pairToPriceStore`'s count and `pairPriceToOrderStore`'s actual keys), `pairPriceToOrderStore.get(pairPriceKey)` returns a capsule whose internal `orderIdList` field is `null` because the parse silently failed with an uncaught `NullPointerException` thrown before the constructor even returns. That NPE is unhandled by the actuator and propagates through `Manager`'s transaction/block-processing path.

### Impact Explanation
An uncaught `NullPointerException` thrown while executing a transaction contract inside block application is not part of the actuators' declared/caught exception set. Since all full nodes deterministically execute the same transactions when applying a block, this crashes every node processing that block identically — this is a chain-wide denial of service / node halt, directly analogous to the CVE's "NULL pointer dereference and BUG" leading to a crash from a specific state reached via ordinary user-issued operations (in java-tron's case, ordinary `MarketCancelOrderContract`/`MarketSellAssetContract` transactions rather than KVM ioctls).

### Likelihood Explanation
The precondition (the `pairPriceKey` computed from a still-"active" order's fields having no corresponding entry in `pairPriceToOrderStore`) requires a state inconsistency between order state, `pairToPriceStore` counts, and `pairPriceToOrderStore` contents. I was not able to fully verify, within the available tooling and time, a concrete deterministic transaction sequence that provably produces this exact desynchronization from the market-exchange actuators' logic alone (the linked-list maintenance code in `MarketOrderIdListCapsule.removeOrder`/`addOrder` and the price-count bookkeeping in the actuators appear to keep these stores consistent in the paths I reviewed). The clear, confirmed defect is the missing null-check/exception-swallowing in `MarketPairPriceToOrderStore.get()` and `MarketOrderIdListCapsule(byte[])` — this is a real code smell that removes a safety net that other stores in the codebase rely on (`getUnchecked` returning null explicitly, callers checking `!= null` before use, as seen in `MarketSellAssetActuator.saveRemainOrder`). Whether an unprivileged actor can trigger the missing-key precondition needs further live/dynamic verification (e.g., fuzzing concurrent cancel/match transactions within the same block) before treating this as a fully proven, directly triggerable Medium/High severity issue.

### Recommendation
- Make `MarketPairPriceToOrderStore.get()` consistent with the rest of the codebase: check `value == null` and throw `ItemNotFoundException` (matching the interface contract), instead of constructing a capsule from a null byte array.
- Harden `MarketOrderIdListCapsule(byte[] data)` to explicitly guard against `data == null` before calling `parseFrom`, and to not silently swallow parse failures that leave `orderIdList` uninitialized (`null`) — e.g., throw a well-defined exception or initialize to an empty list so downstream `isOrderEmpty()`/`getHead()` calls do not NPE.
- Audit all call sites (`MarketCancelOrderActuator`, `MarketSellAssetActuator`) to add explicit existence checks before calling `orderIdListCapsule.removeOrder(...)`/`isOrderEmpty()`, and ensure any legitimately-possible "missing bucket" case is turned into a caught `ContractExeException`/`ItemNotFoundException` rather than an unhandled `NullPointerException`.

### Proof of Concept
Not fully reproducible with static analysis alone. The concrete unsafe code path is:
1. Call `MarketPairPriceToOrderStore.get(key)` with a `key` that does not exist in `revokingDB` (confirmed by reading the override; no existence check present).
2. `revokingDB.get(key)` returns `null`.
3. `new MarketOrderIdListCapsule(null)` invokes `MarketOrderIdList.parseFrom(null)`, which throws an uncaught `NullPointerException` from within the constructor (the constructor's `catch` only handles `InvalidProtocolBufferException`).
4. This exception is not in the `catch` clause of `MarketCancelOrderActuator.execute()` / not guarded in `MarketSellAssetActuator.matchOrder()`, so it propagates out of actuator execution during block application.

A full end-to-end PoC requires demonstrating a live transaction sequence that produces a "missing pairPriceKey while order is still active" state — I could not confirm such a sequence exists within the reviewed matching/cancellation logic in the time available, so this should be verified with targeted testing/fuzzing (e.g., interleaving `MarketSellAssetContract` matches and `MarketCancelOrderContract` cancellations for orders at the same price within a single block) before treating the full chain-halt scenario as proven.

### Citations

**File:** chainbase/src/main/java/org/tron/core/store/MarketPairPriceToOrderStore.java (L24-28)
```java
  @Override
  public MarketOrderIdListCapsule get(byte[] key) throws ItemNotFoundException {
    byte[] value = revokingDB.get(key);
    return new MarketOrderIdListCapsule(value);
  }
```

**File:** chainbase/src/main/java/org/tron/core/capsule/MarketOrderIdListCapsule.java (L24-30)
```java
  public MarketOrderIdListCapsule(final byte[] data) {
    try {
      this.orderIdList = MarketOrderIdList.parseFrom(data);
    } catch (InvalidProtocolBufferException e) {
      logger.debug(e.getMessage(), e);
    }
  }
```

**File:** actuator/src/main/java/org/tron/core/actuator/MarketCancelOrderActuator.java (L111-121)
```java
      // 2. clear orderList
      byte[] pairPriceKey = MarketUtils.createPairPriceKey(
          orderCapsule.getSellTokenId(),
          orderCapsule.getBuyTokenId(),
          orderCapsule.getSellTokenQuantity(),
          orderCapsule.getBuyTokenQuantity()
      );
      MarketOrderIdListCapsule orderIdListCapsule = pairPriceToOrderStore.get(pairPriceKey);

      // delete order
      orderIdListCapsule.removeOrder(orderCapsule, orderStore, pairPriceKey, pairPriceToOrderStore);
```

**File:** actuator/src/main/java/org/tron/core/actuator/MarketCancelOrderActuator.java (L141-147)
```java
    } catch (ItemNotFoundException
        | InvalidProtocolBufferException
        | BalanceInsufficientException e) {
      logger.debug(e.getMessage(), e);
      ret.setStatus(fee, code.FAILED);
      throw new ContractExeException(e.getMessage());
    }
```

**File:** actuator/src/main/java/org/tron/core/actuator/MarketCancelOrderActuator.java (L195-210)
```java
    // Whether the order exist
    MarketOrderCapsule marketOrderCapsule;
    try {
      marketOrderCapsule = orderStore.get(orderId.toByteArray());
    } catch (ItemNotFoundException ex) {
      throw new ContractValidateException(
          "orderId not exists");
    }

    if (!marketOrderCapsule.isActive()) {
      throw new ContractValidateException("Order is not active!");
    }

    if (!marketOrderCapsule.getOwnerAddress().equals(ownerAccount.getAddress())) {
      throw new ContractValidateException("Order does not belong to the account!");
    }
```

**File:** actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java (L336-339)
```java
      byte[] pairPriceKey = priceKeysList.get(0);

      // if not exists
      MarketOrderIdListCapsule orderIdListCapsule = pairPriceToOrderStore.get(pairPriceKey);
```
