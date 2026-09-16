### Title
Unhandled NullPointerException in `MarketSellAssetActuator` when a maker's account is deleted before order matching, causing node crash / chain halt - (File: `actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java`)

### Summary
This is the same bug class as the reported Aave Lens issue: a stale reference to an account that has since ceased to exist (there, a burned `referrerProfileId`; here, a market-order maker's `ownerAddress`) is dereferenced without an existence check, causing the transaction that touches it to fail in an uncontrolled way. In java-tron this manifests as an unhandled `NullPointerException` rather than a clean revert, because `AccountStore.get()` returns `null` for non-existent accounts (unlike Solidity's `ownerOf`, which reverts). Since the failing code runs inside `MarketSellAssetActuator.execute()` during deterministic block application (`Manager.processBlock` → `processTransaction`), and the actuator's catch clause does not handle `RuntimeException`/`NullPointerException`, the failure propagates uncaught through the block-application path.

### Finding Description
`MarketSellAssetContract` lets any account place a sell order, and there is no check preventing a smart-contract account from being the order owner (unlike `DelegateResourceActuator`, which explicitly forbids delegating resources to `AccountType.Contract` addresses, see [1](#0-0) ). A contract account can therefore place a maker order via `MarketSellAssetActuator`, which stores the order keyed by `ownerAddress` in `orderStore`/`pairPriceToOrderStore` [2](#0-1) .

Later, that contract account can self-destruct via `SUICIDE`/`SUICIDE2`, which removes the account entirely from `AccountStore`: `deleteContract()` calls `getAccountStore().delete(address)` [3](#0-2) , and this is confirmed by test assertions that `accountStore.get(contractAddr)` is `null` after suicide [4](#0-3) .

When a subsequent taker's `MarketSellAssetContract` matches against this now-orphaned maker order, `matchSingleOrder()` calls `addTrxOrToken(makerOrderCapsule, makerBuyTokenQuantityReceive)` (the single-argument overload for the maker side) [5](#0-4) . That method fetches the maker's account without any null check and immediately calls a mutator on it:

```java
private void addTrxOrToken(MarketOrderCapsule orderCapsule, long num) {
  AccountCapsule accountCapsule = accountStore
      .get(orderCapsule.getOwnerAddress().toByteArray());
  ...
  accountCapsule.setBalance(...); // or addAssetAmountV2(...)
``` [6](#0-5) 

Because `accountCapsule` is `null`, this throws an unhandled `NullPointerException`. The same pattern exists in `returnSellTokenRemain()` [7](#0-6) , which is invoked when the matched quantity rounds to zero.

Critically, `execute()`'s catch block only handles a fixed set of checked exceptions:
```java
} catch (ItemNotFoundException
    | InvalidProtocolBufferException
    | BalanceInsufficientException
    | ContractValidateException e) {
``` [8](#0-7) 
`NullPointerException` is not caught here, so it propagates out of `execute()`, which is invoked directly from `Manager.processTransaction()` (via the actuator list) during `Manager.processBlock()` for every transaction in a block [9](#0-8) . This is a deterministic, consensus-critical code path executed by every full node applying the block, unlike the Lens report where only the transaction's own `collect()` reverts.

### Impact Explanation
Because the same block is replayed by every node on the network, an uncaught `NullPointerException` in this deterministic path is not merely a single failed transaction — it is thrown identically on every node processing the block. Depending on how the top-level block-processing loop (`pushBlock`/HTTP or P2P block-application callers) handles an unchecked `RuntimeException` that is not declared in `applyBlock`'s throws clause, this can crash the node process or otherwise halt correct block application, since the exception is not part of the expected checked-exception contract (`ContractExeException`, `ContractValidateException`, etc.) that callers are built to catch and recover from. At minimum it denies service for the affected market pair going forward (orders referencing the deleted maker can never be matched again, permanently locking the taker's fill path); at worst it is a network-wide DoS/crash vector reachable by an ordinary user via three fully permissionless, unprivileged transactions (place a sell order as a contract, self-destruct the contract, trigger a matching sell order from any other account).

### Likelihood Explanation
All three steps are reachable by any unprivileged transaction broadcaster: (1) deploy a trivial contract and have it submit a `MarketSellAssetContract` to post a maker order — no validation forbids contract owners; (2) trigger `SUICIDE`/`SUICIDE2` on that contract, removing its `AccountCapsule` from `AccountStore`; (3) submit a matching `MarketSellAssetContract` from any account so that the matching engine walks into the deleted maker's order and calls `addTrxOrToken`/`returnSellTokenRemain`. No special permissions, no SR/witness collusion, and no p2p/network manipulation are required — a single user's own transactions can trigger it end-to-end.

### Recommendation
In `MarketSellAssetActuator.addTrxOrToken(MarketOrderCapsule, long)` and `returnSellTokenRemain(MarketOrderCapsule)`, check whether `accountStore.get(orderCapsule.getOwnerAddress().toByteArray())` returns `null` before mutating it. If the maker's account no longer exists, treat it analogously to how the original report recommends handling burned referrers: skip the transfer/return gracefully (e.g., recreate a minimal account, redirect to the black-hole/treasury, or cancel/void the stale order defensively) and emit a log/event, rather than deferencing a null `AccountCapsule`. Additionally, consider disallowing contract accounts (`AccountType.Contract`) from being market order owners in `MarketSellAssetActuator.validate()`, mirroring the existing restriction in `DelegateResourceActuator`, or ensure orders belonging to self-destructed accounts are purged from the order book at suicide time.

### Proof of Concept
1. Deploy a contract `C` and, from `C`, submit `MarketSellAssetContract(ownerAddress=C, sellTokenId=A, buyTokenId=TRX, ...)` to place a maker sell order; it gets stored via `createAndSaveOrder`/`pairPriceToOrderStore`.
2. From `C`, execute `SUICIDE`/`SUICIDE2` to a different beneficiary address; this deletes `C`'s `AccountCapsule` from `AccountStore` (confirmed by `RepositoryImpl.deleteContract` and test `Assert.assertNull(accountStore.get(contractAddr))`).
3. From any other account `D`, submit a `MarketSellAssetContract` that matches the price/pair of `C`'s outstanding order (sell TRX for A at the corresponding price).
4. During `matchOrder`/`matchSingleOrder`, the engine calls `addTrxOrToken(makerOrderCapsule, makerBuyTokenQuantityReceive)`, which does `accountStore.get(C)` → `null`, then calls `.setBalance(...)`/`.addAssetAmountV2(...)` on the null reference, throwing an uncaught `NullPointerException` inside `execute()`, which is not caught by the actuator's catch clause and propagates out of `Manager.processTransaction`/`processBlock`.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/DelegateResourceActuator.java (L243-246)
```java
    if (receiverCapsule.getType() == AccountType.Contract) {
      throw new ContractValidateException(
          "Do not allow delegate resources to contract addresses");
    }
```

**File:** actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java (L152-155)
```java
    } catch (ItemNotFoundException
        | InvalidProtocolBufferException
        | BalanceInsufficientException
        | ContractValidateException e) {
```

**File:** actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java (L490-490)
```java
    addTrxOrToken(makerOrderCapsule, makerBuyTokenQuantityReceive);
```

**File:** actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java (L501-524)
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

    long now = dynamicStore.getLatestBlockHeaderTimestamp();
    orderCapsule.setCreateTime(now);

    marketAccountOrderCapsule.addOrders(orderCapsule.getID());
    marketAccountOrderCapsule.setCount(marketAccountOrderCapsule.getCount() + 1);
    marketAccountOrderCapsule.setTotalCount(marketAccountOrderCapsule.getTotalCount() + 1);
    marketAccountStore.put(accountCapsule.createDbKey(), marketAccountOrderCapsule);
    orderStore.put(orderId, orderCapsule);

    return orderCapsule;
```

**File:** actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java (L550-562)
```java
  private void addTrxOrToken(MarketOrderCapsule orderCapsule, long num) {
    AccountCapsule accountCapsule = accountStore
        .get(orderCapsule.getOwnerAddress().toByteArray());

    byte[] buyTokenId = orderCapsule.getBuyTokenId();
    if (Arrays.equals(buyTokenId, "_".getBytes())) {
      accountCapsule.setBalance(addExact(accountCapsule.getBalance(), num));
    } else {
      accountCapsule
          .addAssetAmountV2(buyTokenId, num, dynamicStore, assetIssueStore);
    }
    accountStore.put(orderCapsule.getOwnerAddress().toByteArray(), accountCapsule);
  }
```

**File:** actuator/src/main/java/org/tron/core/actuator/MarketSellAssetActuator.java (L564-570)
```java
  private void returnSellTokenRemain(MarketOrderCapsule orderCapsule) {
    AccountCapsule accountCapsule = accountStore
        .get(orderCapsule.getOwnerAddress().toByteArray());

    MarketUtils.returnSellTokenRemain(orderCapsule, accountCapsule, dynamicStore, assetIssueStore);
    accountStore.put(orderCapsule.getOwnerAddress().toByteArray(), accountCapsule);
  }
```

**File:** actuator/src/main/java/org/tron/core/vm/repository/RepositoryImpl.java (L487-492)
```java
  @Override
  public void deleteContract(byte[] address) {
    getCodeStore().delete(address);
    getAccountStore().delete(address);
    getContractStore().delete(address);
  }
```

**File:** framework/src/test/java/org/tron/common/runtime/ProgramResultTest.java (L500-502)
```java
    Assert
        .assertEquals(dbManager.getAccountStore().get(Hex.decode(TRANSFER_TO)).getBalance(), 1000);
    Assert.assertNull(dbManager.getAccountStore().get(suicideContract));
```

**File:** framework/src/main/java/org/tron/core/db/Manager.java (L1884-1898)
```java
      for (TransactionCapsule transactionCapsule : block.getTransactions()) {
        rejectExchangeTransaction(transactionCapsule.getInstance());
        if (chainBaseManager.getDynamicPropertiesStore().allowConsensusLogicOptimization()
            && transactionCapsule.retCountIsGreatThanContractCount()) {
          throw new BadBlockException(String.format("The result count %d of this transaction %s is "
                  + "greater than its contract count %d", transactionCapsule.getRetCount(),
              transactionCapsule.getTransactionId(), transactionCapsule.getContractCount()));
        }
        transactionCapsule.setBlockNum(num);
        if (block.generatedByMyself) {
          transactionCapsule.setVerified(true);
        }
        accountStateCallBack.preExeTrans();
        TransactionInfo result = processTransaction(transactionCapsule, block);
        accountStateCallBack.exeTransFinish();
```
