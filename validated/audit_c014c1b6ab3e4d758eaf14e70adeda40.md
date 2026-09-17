### Title
Increasing `MARKET_CANCEL_FEE` can permanently lock funds already escrowed in open market orders - (File: `actuator/src/main/java/org/tron/core/actuator/MarketCancelOrderActuator.java`)

### Summary
`MarketCancelOrderActuator` is the only way for an order placer to reclaim assets escrowed in an open TRC10 market order. The fee required to cancel is computed dynamically at cancel time from the current `MARKET_CANCEL_FEE` chain parameter rather than being reserved/escrowed when the order was created. If the committee raises `MARKET_CANCEL_FEE` via a normal `ProposalCreateContract`/`ProposalApproveContract` governance flow after orders are already open, an order owner whose spendable TRX balance is below the new fee can never again satisfy the balance check in `validate()`, permanently freezing the assets locked in that order.

### Finding Description
When a sell order is placed (`MarketSellAssetActuator`), the seller's tokens/TRX are locked into a `MarketOrderCapsule` that lives in `MarketOrderStore`/`MarketPairPriceToOrderStore`. The only way to get those assets back before a match is `MarketCancelOrderContract`, processed by `MarketCancelOrderActuator`.

In `validate()`:
```
long fee = calcFee();
if (ownerAccount.getBalance() < fee) {
  throw new ContractValidateException("No enough balance !");
}
``` [1](#0-0) 

and in `execute()` the fee is subtracted from the owner's spendable balance before the escrowed sell-token remainder is returned:
```
accountCapsule.setBalance(accountCapsule.getBalance() - fee);
...
MarketUtils.returnSellTokenRemain(orderCapsule, accountCapsule, dynamicStore, assetIssueStore);
``` [2](#0-1) 

`calcFee()`/tests confirm the fee used is the *current* dynamic-store value, not a value captured at order-creation time — e.g. `MarketCancelOrderActuatorTest` asserts the deducted amount equals `dbManager.getDynamicPropertiesStore().getMarketCancelFee()` at cancel time, not at order-placement time [3](#0-2) .

`MARKET_CANCEL_FEE` is a committee-governed dynamic parameter that can be raised up to `10_000_000_000L` (10,000 TRX) at any time by a passed proposal:
```
case MARKET_CANCEL_FEE: {
  ...
  if (value < 0 || value > 10_000_000_000L) {
    throw new ContractValidateException(
        "Bad MARKET_CANCEL_FEE parameter value, valid range is [0,10_000_000_000L]");
  }
  break;
}
``` [4](#0-3) 
and is written into `DynamicPropertiesStore` immediately upon proposal execution with no grandfathering for already-open orders:
```
case MARKET_CANCEL_FEE: {
  manager.getDynamicPropertiesStore().saveMarketCancelFee(entry.getValue());
  break;
}
``` [5](#0-4) 

Because the fee is neither reserved at order-placement time nor capped/escrowed against the order, any account that placed an order while the fee was low and then spent down its liquid balance (a completely normal and expected sequence of events for an active trader) can be locked out of `MarketCancelOrderActuator.validate()` once the fee rises above its current balance. There is no alternate path in the codebase to withdraw the sell-token remainder escrowed in `MarketOrderCapsule` other than this actuator, so the locked assets become permanently unrecoverable until/unless the owner's balance happens to exceed the (possibly increased again) fee.

This mirrors the reported Y2K `Carousel` bug class exactly: a fee parameter that is re-evaluated against previously-committed/queued user state (deposit queue in Y2K, escrowed order balance here) rather than fixed at commitment time, so a legitimate fee increase can strand already-committed user funds.

### Impact Explanation
Any order placer's escrowed sell-token/TRX balance in an open market order can become permanently unrecoverable if `MARKET_CANCEL_FEE` is later raised above the placer's current liquid TRX balance, since `MarketCancelOrderActuator` — the sole redemption path — will always revert with "No enough balance !". This is a permanent freezing-of-funds condition affecting any market participant, satisfying the High-severity bar (permanent freezing of user funds).

### Likelihood Explanation
`MARKET_CANCEL_FEE` changes are a normal, expected governance action (not privileged abuse) — the same trust model as the original Y2K report's admin fee change. Any active trader who places several orders and later spends most of their liquid TRX (a very common pattern) is exposed the moment such a proposal passes. No attacker collusion is required; it is a systemic design flaw in fee timing, reachable purely through ordinary `MarketSellAssetContract`/`MarketCancelOrderContract` transactions from an unprivileged order placer.

### Recommendation
Reserve/escrow the cancel fee (or cap it) at order-creation time instead of recomputing it dynamically at cancellation, or allow cancellation to proceed with the fee capped at the account's available balance / charged from the returned escrow itself rather than from the free balance, so that a later increase in `MARKET_CANCEL_FEE` cannot strand already-open orders.

### Proof of Concept
1. Order placer submits `MarketSellAssetContract` while `MARKET_CANCEL_FEE` = X, locking sell-token quantity into a `MarketOrderCapsule` (`MarketSellAssetActuator`).
2. Placer's liquid TRX balance subsequently drops to `Y` where `X < Y` (normal usage — e.g. transfers, other fees).
3. Committee passes a proposal raising `MARKET_CANCEL_FEE` to `Z` where `Z > Y` (allowed up to 10,000 TRX per `ProposalUtil.validator` case `MARKET_CANCEL_FEE`), applied via `ProposalService.process` -> `saveMarketCancelFee`.
4. Placer submits `MarketCancelOrderContract` to reclaim escrowed tokens; `MarketCancelOrderActuator.validate()` computes `fee = calcFee()` = `Z` and throws `ContractValidateException("No enough balance !")` since `ownerAccount.getBalance() (Y) < fee (Z)`.
5. The order remains open indefinitely and the escrowed sell-token amount is unrecoverable by the owner unless their balance grows beyond `Z`, which is outside their control if the fee is raised further.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/MarketCancelOrderActuator.java (L93-106)
```java
      byte[] orderId = contract.getOrderId().toByteArray();
      MarketOrderCapsule orderCapsule = orderStore.get(orderId);

      // fee
      accountCapsule.setBalance(accountCapsule.getBalance() - fee);
      if (dynamicStore.supportBlackHoleOptimization()) {
        dynamicStore.burnTrx(fee);
      } else {
        adjustBalance(accountStore, accountStore.getBlackhole(), fee);
      }
      // 1. return balance and token
      MarketUtils
          .returnSellTokenRemain(orderCapsule, accountCapsule, dynamicStore, assetIssueStore);

```

**File:** actuator/src/main/java/org/tron/core/actuator/MarketCancelOrderActuator.java (L211-217)
```java

    // Whether the balance is enough
    long fee = calcFee();
    if (ownerAccount.getBalance() < fee) {
      throw new ContractValidateException("No enough balance !");
    }

```

**File:** framework/src/test/java/org/tron/core/actuator/MarketCancelOrderActuatorTest.java (L551-557)
```java
    //check balance
    accountCapsule = dbManager.getAccountStore()
        .get(ByteArray.fromHexString(OWNER_ADDRESS_FIRST));

    Assert.assertEquals(
        balanceBefore + 100L - dbManager.getDynamicPropertiesStore().getMarketCancelFee(),
        +accountCapsule.getBalance());
```

**File:** actuator/src/main/java/org/tron/core/utils/ProposalUtil.java (L384-397)
```java
      case MARKET_CANCEL_FEE: {
        if (!forkController.pass(ForkBlockVersionEnum.VERSION_4_1)) {
          throw new ContractValidateException("Bad chain parameter id [MARKET_CANCEL_FEE]");
        }
        if (!dynamicPropertiesStore.supportAllowMarketTransaction()) {
          throw new ContractValidateException(
              "Market Transaction is not activated, can not set Market Cancel Fee");
        }
        if (value < 0 || value > 10_000_000_000L) {
          throw new ContractValidateException(
              "Bad MARKET_CANCEL_FEE parameter value, valid range is [0,10_000_000_000L]");
        }
        break;
      }
```

**File:** framework/src/main/java/org/tron/core/consensus/ProposalService.java (L232-235)
```java
        case MARKET_CANCEL_FEE: {
          manager.getDynamicPropertiesStore().saveMarketCancelFee(entry.getValue());
          break;
        }
```
