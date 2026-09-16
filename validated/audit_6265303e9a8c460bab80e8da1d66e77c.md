Based on my investigation, I found no valid analog of this bug class in java-tron.

The reported vulnerability is a specific Solidity anti-pattern: an array where the index is used as an implicit identifier (NFT token ID), and removal is done by swapping the target element with the last array element and popping, which corrupts the identifier-to-data mapping for the element that gets moved.

I searched java-tron's actuators and capsules for similar array-index-as-identifier removal patterns across the reachable areas (unfreeze/freeze balance lists, witness/vote lists, market order lists, asset lists):

- `UnfreezeAssetActuator`, `UnfreezeBalanceActuator`, `UnfreezeBalanceV2Actuator`, and `UnfreezeBalanceV2Processor` all remove expired `Frozen`/`UnFreezeV2` entries using `Iterator.remove()` on an `ArrayList` copy, then rebuild the protobuf field with `.clearX().addAllX(list)`. This preserves order correctly and does not tie any external identifier to array position. [1](#0-0) [2](#0-1) 
- `AccountCapsule.addFrozenV2List`/`updateFrozenV2List` index into `FrozenV2List` by `ResourceCode` type, not by an externally-referenced token ID, and there's no removal that swaps with the last element. [3](#0-2) 
- `MarketAccountOrderCapsule.removeOrder` uses `List.remove(Object)` (value-based removal by order ID, not index-based swap-and-pop), so no identifier corruption occurs. [4](#0-3) 
- `MarketOrderIdListCapsule.removeOrder` implements a proper doubly-linked-list unlink (updating `prev`/`next` pointers of head/tail), not an array-position swap. [5](#0-4) 

Java-tron's data model doesn't use arrays where an element's array index doubles as its externally-referenced ID (the way Solidity's ERC-721 balance array is tied to NFT `tokenId`). All list removals I found in the reachable transaction/actuator paths either use `Iterator.remove()` (which shifts elements, preserving no positional identity that anything depends on), value-based `List.remove(Object)`, or explicit linked-list pointer updates — none of which reproduce the "swap last element into removed slot" bug class described in the report.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/UnfreezeBalanceActuator.java (L196-210)
```java
          List<Frozen> frozenList = Lists.newArrayList();
          frozenList.addAll(accountCapsule.getFrozenList());
          Iterator<Frozen> iterator = frozenList.iterator();
          long now = dynamicStore.getLatestBlockHeaderTimestamp();
          while (iterator.hasNext()) {
            Frozen next = iterator.next();
            if (next.getExpireTime() <= now) {
              unfreezeBalance += next.getFrozenBalance();
              iterator.remove();
            }
          }

          accountCapsule.setInstance(accountCapsule.getInstance().toBuilder()
              .setBalance(oldBalance + unfreezeBalance)
              .clearFrozen().addAllFrozen(frozenList).build());
```

**File:** actuator/src/main/java/org/tron/core/vm/nativecontract/UnfreezeBalanceV2Processor.java (L153-174)
```java
  private long unfreezeExpire(AccountCapsule accountCapsule, long now) {
    long unfreezeBalance = 0L;

    List<Protocol.Account.UnFreezeV2> unFrozenV2List = Lists.newArrayList();
    unFrozenV2List.addAll(accountCapsule.getUnfrozenV2List());
    Iterator<Protocol.Account.UnFreezeV2> iterator = unFrozenV2List.iterator();

    while (iterator.hasNext()) {
      Protocol.Account.UnFreezeV2 next = iterator.next();
      if (next.getUnfreezeExpireTime() <= now) {
        unfreezeBalance += next.getUnfreezeAmount();
        iterator.remove();
      }
    }

    accountCapsule.setInstance(
        accountCapsule.getInstance().toBuilder()
            .setBalance(accountCapsule.getBalance() + unfreezeBalance)
            .clearUnfrozenV2()
            .addAllUnfrozenV2(unFrozenV2List).build()
    );
    return unfreezeBalance;
```

**File:** chainbase/src/main/java/org/tron/core/capsule/AccountCapsule.java (L545-567)
```java
  private void addFrozenBalanceForResource(ResourceCode type, long balance) {
    boolean doUpdate = false;
    for (int i = 0; i < this.account.getFrozenV2List().size(); i++) {
      if (this.account.getFrozenV2List().get(i).getType().equals(type)) {
        long newAmount = this.account.getFrozenV2(i).getAmount() + balance;
        FreezeV2 freezeV2 = FreezeV2.newBuilder()
                .setType(type)
                .setAmount(newAmount)
                .build();
        this.updateFrozenV2List(i, freezeV2);
        doUpdate = true;
        break;
      }
    }

    if (!doUpdate) {
      FreezeV2 freezeV2 = FreezeV2.newBuilder()
              .setType(type)
              .setAmount(balance)
              .build();
      this.addFrozenV2List(freezeV2);
    }
  }
```

**File:** chainbase/src/main/java/org/tron/core/capsule/MarketAccountOrderCapsule.java (L62-74)
```java
  public void removeOrder(ByteString orderId) {
    List<ByteString> orderList = Lists.newArrayList();
    orderList.addAll(this.getOrdersList());
    orderList.remove(orderId);

    this.accountOrder = this.accountOrder.toBuilder()
        .setCount(this.getCount() - 1)
        .clearOrders()
        .addAllOrders(orderList)
        .build();


  }
```

**File:** chainbase/src/main/java/org/tron/core/capsule/MarketOrderIdListCapsule.java (L78-133)
```java
  public void removeOrder(MarketOrderCapsule currentCapsule, MarketOrderStore marketOrderStore,
      byte[] pairPriceKey, MarketPairPriceToOrderStore pairPriceToOrderStore)
      throws ItemNotFoundException {
    MarketOrderCapsule preCapsule = currentCapsule.getPrevCapsule(marketOrderStore);
    MarketOrderCapsule nextCapsule = currentCapsule.getNextCapsule(marketOrderStore);

    // pre.next = current.next
    // current.next.prev = current.prev
    if (preCapsule != null) {
      if (nextCapsule != null) {
        preCapsule.setNext(currentCapsule.getNext());
      } else {
        preCapsule.setNext(new byte[0]);
      }

      marketOrderStore.put(preCapsule.getID().toByteArray(), preCapsule);
    } else {
      // current is head
      // head = current.next
      if (nextCapsule != null) {
        this.setHead(currentCapsule.getNext());
      } else {
        // need to delete, outside
        this.setHead(new byte[0]);
      }

      // head changed
      pairPriceToOrderStore.put(pairPriceKey, this);
    }

    if (nextCapsule != null) {
      if (preCapsule != null) {
        nextCapsule.setPrev(currentCapsule.getPrev());
      } else {
        nextCapsule.setPrev(new byte[0]);
      }

      marketOrderStore.put(nextCapsule.getID().toByteArray(), nextCapsule);
    } else {
      // current is tail
      // this.tail = pre
      if (preCapsule != null) {
        this.setTail(currentCapsule.getPrev());
      } else {
        this.setTail(new byte[0]);
      }

      // tail changed
      pairPriceToOrderStore.put(pairPriceKey, this);
    }

    // update current
    currentCapsule.setPrev(new byte[0]);
    currentCapsule.setNext(new byte[0]);
    marketOrderStore.put(currentCapsule.getID().toByteArray(), currentCapsule);
  }
```
