### Title
Unrefunded truncation loss in `ParticipateAssetIssueContract` lets a buyer pay strictly more TRX than another buyer for the same amount of asset tokens - ([File: actuator/src/main/java/org/tron/core/actuator/ParticipateAssetIssueActuator.java])

### Summary
`ParticipateAssetIssueActuator` converts a buyer-supplied TRX amount (`cost`) into an asset amount using integer floor division (`exchangeAmount = floor(cost * num / trxNum)`), but it debits the buyer's full `cost` and credits the issuer the full `cost`, never adjusting the charge to match the truncated (floored) output. This is the same root cause as the reported `SolverVaults::deposit` bug: two callers presenting different raw input amounts can receive an identical output amount, while the extra input is not refunded — it is unconditionally transferred to the counterparty.

### Finding Description
In `ParticipateAssetIssueActuator.execute()`: [1](#0-0) 

```
long cost = participateAssetIssueContract.getAmount();
...
long balance = subtractExact(ownerAccount.getBalance(), cost);   // buyer pays full cost
...
long exchangeAmount = multiplyExact(cost, assetIssueCapsule.getNum());
exchangeAmount = floorDiv(exchangeAmount, assetIssueCapsule.getTrxNum());   // floor-truncated output
ownerAccount.addAssetAmountV2(key, exchangeAmount, ...);
...
toAccount.setBalance(addExact(toAccount.getBalance(), cost));   // issuer receives full cost, not the pro-rata portion
```

`trxNum` and `num` are the exchange-rate parameters that an asset issuer sets at asset-creation time (`AssetIssueContract.trx_num` / `num`), and are not bounded to small values. Because `exchangeAmount` uses `floorDiv`, whenever `cost` is not an exact multiple of `trxNum/num`'s reduced ratio, the buyer's TRX is rounded down to the nearest whole asset unit, and the "leftover" fractional TRX (up to `trxNum - 1` drops per unit-purchase, scaled by `cost`) is not returned to the buyer — it is transferred in full to the token issuer's balance via `toAccount.setBalance(addExact(toAccount.getBalance(), cost))`.

This is exactly the SolverVaults bug class: the deposited/paid amount is transferred in full while the minted/credited output amount is computed with a truncating division, and the difference is never accounted for or refunded. Consequently, for a given `trxNum`/`num` ratio, two different buyers can submit two different `cost` values yet receive the exact same `exchangeAmount` of the asset — the buyer who submitted the larger `cost` pays strictly more TRX for the same output. The project's own test suite explicitly documents and accepts this truncation ("SameTokenName ... exchange devisible" tests), confirming the behavior is real and reachable, not merely theoretical. [2](#0-1) 

### Impact Explanation
Any ordinary user (asset issuer) can create an `AssetIssueContract` with an arbitrarily large `trx_num` value (bounded only by `int32`), and any ordinary buyer who calls `ParticipateAssetIssueContract` with a `cost` that is not an exact multiple relative to `trx_num`/`num` will have up to `trx_num - 1` "wasted" drops of TRX transferred to the issuer with zero corresponding asset tokens issued. Because `trx_num` is an `int32` field with no enforced upper bound in `AssetIssueActuator` validation (only observed via `getNum()`/`getTrxNum()` overflow checks, not magnitude caps), the truncation window and thus the unrecoverable TRX loss per transaction can be made large by a malicious issuer, causing buyers to systematically overpay TRX relative to what other buyers pay for the identical amount of the purchased asset — a concrete loss of funds for the disadvantaged buyer and an unearned gain for the issuer.

### Likelihood Explanation
This is triggered by a completely standard, unprivileged transaction: any account can issue an asset with a chosen `trx_num`/`num` pair, and any account can call `participateAssetIssue` with an arbitrary `amount`. No special permissions, timing, or race conditions are required — the discrepancy manifests deterministically from the floor-division arithmetic on every non-exact-multiple purchase.

### Recommendation
Recompute the TRX amount actually consumed based on the truncated `exchangeAmount` before moving balances, i.e.:
1. Compute `exchangeAmount = floor(cost * num / trxNum)`.
2. Compute `actualCost = ceil(exchangeAmount * trxNum / num)` (or equivalently, only charge `cost - (cost * num % trxNum) * trxNum / num`) and refund the buyer the difference between the originally supplied `cost` and `actualCost`, or reject the transaction unless `cost` is an exact multiple of the effective ratio.
3. Transfer only `actualCost` to the issuer's balance instead of the full `cost`, or refund the remainder to the buyer.

### Proof of Concept
Given an asset issued with `trx_num = 1_000_000`, `num = 1`:
- Buyer A sends `cost = 1` (drop of TRX): `exchangeAmount = floor(1*1/1_000_000) = 0` — buyer A loses 1 drop for 0 tokens, issuer gains 1 drop.
- Buyer B sends `cost = 999_999`: `exchangeAmount = floor(999_999*1/1_000_000) = 0` — buyer B loses 999,999 drops for the same 0 tokens that buyer A received for only 1 drop, and the issuer keeps the entire 999,999 drops.

This mirrors the reported PoC pattern (`User1` and `User2` paying different amounts for the same output quantity), confirmed by the existing project test `sameTokenNameCloseExchangeDevisibleTest` / `sameTokenNameOpenExchangeDevisibleTest`, which already demonstrates the truncating division without any refund mechanism. [3](#0-2)

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/ParticipateAssetIssueActuator.java (L62-90)
```java
      long cost = participateAssetIssueContract.getAmount();

      //subtract from owner address
      byte[] ownerAddress = participateAssetIssueContract.getOwnerAddress().toByteArray();
      AccountCapsule ownerAccount = accountStore.get(ownerAddress);
      long balance = subtractExact(ownerAccount.getBalance(), cost);
      balance = subtractExact(balance, fee);
      ownerAccount.setBalance(balance);
      byte[] key = participateAssetIssueContract.getAssetName().toByteArray();

      //calculate the exchange amount
      AssetIssueCapsule assetIssueCapsule;
      assetIssueCapsule = Commons
          .getAssetIssueStoreFinal(dynamicStore, assetIssueStore, assetIssueV2Store).get(key);

      long exchangeAmount = multiplyExact(cost, assetIssueCapsule.getNum());
      exchangeAmount = floorDiv(exchangeAmount, assetIssueCapsule.getTrxNum());
      ownerAccount.addAssetAmountV2(key, exchangeAmount, dynamicStore, assetIssueStore);

      //add to to_address
      byte[] toAddress = participateAssetIssueContract.getToAddress().toByteArray();
      AccountCapsule toAccount = accountStore.get(toAddress);
      toAccount.setBalance(addExact(toAccount.getBalance(), cost));
      if (!toAccount.reduceAssetAmountV2(key, exchangeAmount, dynamicStore, assetIssueStore)) {
        throw new ContractExeException("reduceAssetAmount failed !");
      }

      //write to db
      accountStore.put(ownerAddress, ownerAccount);
```

**File:** framework/src/test/java/org/tron/core/actuator/ParticipateAssetIssueActuatorTest.java (L536-567)
```java
  /**
   * SameTokenName close, exchange devisible
   */
  @Test
  public void sameTokenNameCloseExchangeDevisibleTest() {
    initAssetIssue(chainBaseManager.getDynamicPropertiesStore()
            .getLatestBlockHeaderTimestamp() - 1000,
        chainBaseManager.getDynamicPropertiesStore().getLatestBlockHeaderTimestamp() + 1000);
    ParticipateAssetIssueActuator actuator = new ParticipateAssetIssueActuator(); //no problem
    actuator.setChainBaseManager(chainBaseManager).setAny(getContract(999L));

    TransactionResultCapsule ret = new TransactionResultCapsule();
    try {
      actuator.validate();
      actuator.execute(ret);

      AccountCapsule owner =
          chainBaseManager.getAccountStore().get(ByteArray.fromHexString(OWNER_ADDRESS));
      AccountCapsule toAccount =
          chainBaseManager.getAccountStore().get(ByteArray.fromHexString(TO_ADDRESS));

      Assert.assertEquals(owner.getAssetMapForTest().get(ASSET_NAME).longValue(),
          (999L * NUM) / TRX_NUM);
      Assert.assertEquals(
          toAccount.getAssetMapForTest().get(ASSET_NAME).longValue(),
          TOTAL_SUPPLY - (999L * NUM) / TRX_NUM);
    } catch (ContractValidateException e) {
      Assert.assertFalse(e instanceof ContractValidateException);
    } catch (ContractExeException e) {
      Assert.assertFalse(e instanceof ContractExeException);
    }
  }
```
