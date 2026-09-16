### Title
Unchecked return value of `addAssetAmountV2` leads to silent asset-credit failure and fund loss - (File: `actuator/src/main/java/org/tron/core/actuator/ParticipateAssetIssueActuator.java`, `actuator/src/main/java/org/tron/core/actuator/TransferAssetActuator.java`)

### Summary
Both `ParticipateAssetIssueActuator.execute()` and `TransferAssetActuator.execute()` call `AccountCapsule.addAssetAmountV2(...)` to credit a TRC10 asset to an account, but never check its boolean return value, while the corresponding `reduceAssetAmountV2(...)` calls in the very same methods are explicitly checked and cause a `ContractExeException` on failure. This is the same class of bug described in the external report — a function that returns a success/failure boolean whose result is silently discarded — applied to java-tron's asset-transfer actuators instead of an ERC20 token wrapper.

### Finding Description
In `ParticipateAssetIssueActuator.execute()`: [1](#0-0) 
the exchange amount is credited to `ownerAccount` via `addAssetAmountV2` with no check on the returned boolean, while the debit from `toAccount` via `reduceAssetAmountV2` a few lines later is checked and throws `ContractExeException` if it fails.

The same asymmetry exists in `TransferAssetActuator.execute()`: [2](#0-1) 
`reduceAssetAmountV2` on the owner side is checked and throws on failure, but `addAssetAmountV2` on the recipient side is not checked at all.

If `addAssetAmountV2` returns `false` (e.g., due to a long-overflow guard on the destination account's asset balance, analogous to the overflow checks exercised in `SameTokenNameCloseAddOverflowTest`/`SameTokenNameOpenAddOverflowTest` for the debit path), the actuator does not roll back, retry, or fail the transaction. Execution continues, the debit (`reduceAssetAmountV2`) still succeeds and removes the asset from the sender/owner side, TRX fee/cost movements are still applied, and the transaction is marked `SUCESS`. The credit to the recipient is silently dropped.

### Impact Explanation
This causes a real accounting inconsistency reachable from an ordinary, unprivileged, signed transaction (`TransferAssetContract` or `ParticipateAssetIssueContract`):
- Assets debited from one account are not credited to the other, resulting in **permanent loss of TRC10 token balance** (tokens effectively burned/vanish from total circulating balance tracked in account state) — a direct violation of asset accounting invariants.
- In `ParticipateAssetIssueActuator`, the participant still pays TRX (`cost`) and the issuer still receives that TRX and has assets deducted from them via `reduceAssetAmountV2`, but the participant may not receive the purchased tokens if the credit silently fails — an unbacked-balance/fund-loss condition for the paying party.

### Likelihood Explanation
Reaching the failure path requires the credited account's existing asset balance plus the incoming amount to trigger the overflow/failure branch inside `addAssetAmountV2`, which is plausible for accounts holding large TRC10 balances (`Long.MAX_VALUE`-adjacent), similar to the overflow scenarios already tested for the debit side (`reduceAssetAmountV2`) in `TransferAssetActuatorTest`. Any account holder can trigger this by directing a transfer or asset-issue participation toward an account they control that is pre-loaded near the overflow boundary, or it can occur unintentionally for high-balance accounts, making it a Medium-likelihood, broadly reachable defect since it requires no special privileges — only a standard broadcastable contract.

### Recommendation
Check the boolean result of `addAssetAmountV2` (and any other capsule mutation method with a boolean success/failure return) in both `TransferAssetActuator.execute()` and `ParticipateAssetIssueActuator.execute()`, throwing `ContractExeException` on failure exactly as is already done for the corresponding `reduceAssetAmountV2` calls, so that a failed credit prevents the paired debit from being committed.

### Proof of Concept
Conceptual PoC (mirrors the existing `SameTokenNameOpenAddOverflowTest` pattern in `framework/src/test/java/org/tron/core/actuator/TransferAssetActuatorTest.java` lines 880-910, but for the currently-unchecked recipient-credit path in `ParticipateAssetIssueActuator`): [3](#0-2) 
1. Set an account's TRC10 asset balance close to `Long.MAX_VALUE`.
2. Trigger a `ParticipateAssetIssueContract` transaction (or `TransferAssetContract`) that would cause the recipient's `addAssetAmountV2` to overflow and return `false`.
3. Observe that the transaction still returns `code.SUCESS`, TRX cost is transferred, and the sender's/issuer's asset balance is already reduced via the checked `reduceAssetAmountV2`, while the intended recipient's asset balance is not incremented — demonstrating silent, permanent token loss caused by ignoring the boolean result of `addAssetAmountV2`.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/ParticipateAssetIssueActuator.java (L77-87)
```java
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
```

**File:** actuator/src/main/java/org/tron/core/actuator/TransferAssetActuator.java (L75-84)
```java
      AccountCapsule ownerAccountCapsule = accountStore.get(ownerAddress);
      if (!ownerAccountCapsule
          .reduceAssetAmountV2(assetName.toByteArray(), amount, dynamicStore, assetIssueStore)) {
        throw new ContractExeException("reduceAssetAmount failed !");
      }
      accountStore.put(ownerAddress, ownerAccountCapsule);

      toAccountCapsule
          .addAssetAmountV2(assetName.toByteArray(), amount, dynamicStore, assetIssueStore);
      accountStore.put(toAddress, toAccountCapsule);
```

**File:** framework/src/test/java/org/tron/core/actuator/TransferAssetActuatorTest.java (L880-910)
```java
  @Test
  public void SameTokenNameOpenAddOverflowTest() {
    createAssertSameTokenNameActive();
    // First, increase the to balance. Else can't complete this test case.
    AccountCapsule toAccount = dbManager.getAccountStore().get(ByteArray.fromHexString(TO_ADDRESS));
    long tokenIdNum = dbManager.getDynamicPropertiesStore().getTokenIdNum();
    toAccount.addAssetV2(ByteArray.fromString(String.valueOf(tokenIdNum)), Long.MAX_VALUE);
    dbManager.getAccountStore().put(ByteArray.fromHexString(TO_ADDRESS), toAccount);
    TransferAssetActuator actuator = new TransferAssetActuator();
    actuator.setChainBaseManager(dbManager.getChainBaseManager()).setAny(getContract(1));

    TransactionResultCapsule ret = new TransactionResultCapsule();
    try {
      actuator.validate();
      actuator.execute(ret);
      Assert.assertTrue(false);
    } catch (ContractValidateException e) {
      Assert.assertTrue(e instanceof ContractValidateException);
      Assert.assertTrue("long overflow".equals(e.getMessage()));
      AccountCapsule owner =
              dbManager.getAccountStore().get(ByteArray.fromHexString(OWNER_ADDRESS));
      toAccount = dbManager.getAccountStore().get(ByteArray.fromHexString(TO_ADDRESS));

      Assert.assertEquals(owner.getAssetV2MapForTest().get(String.valueOf(tokenIdNum)).longValue(),
              OWNER_ASSET_BALANCE);
      Assert.assertEquals(toAccount.getAssetV2MapForTest()
              .get(String.valueOf(tokenIdNum)).longValue(), Long.MAX_VALUE);
    } catch (ContractExeException e) {
      Assert.assertFalse(e instanceof ContractExeException);
    }
  }
```
