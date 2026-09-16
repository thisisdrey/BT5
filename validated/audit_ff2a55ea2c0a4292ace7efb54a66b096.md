### Title
Unchecked `AssetIssueStore.get()` null return causes NullPointerException / uncaught crash path in `ExchangeCreateActuator.execute()` - (File: `actuator/src/main/java/org/tron/core/actuator/ExchangeCreateActuator.java`)

### Summary
`AssetIssueStore.get(byte[])` is implemented to return `null` when the key is absent (it delegates to `getUnchecked`, unlike `ExchangeStore.get()` which throws `ItemNotFoundException`). `ExchangeCreateActuator.execute()` calls this method and immediately dereferences the result without a null check, and `doValidate()` never verifies that the referenced token actually exists in `AssetIssueStore`. This is the same bug class as CVE-2017-6298 (unchecked allocation/lookup return value leading to a null-pointer dereference), reachable from a single unprivileged, signed `ExchangeCreateContract` transaction.

### Finding Description
In `ExchangeCreateActuator.execute()`: [1](#0-0) 
```
//save to new asset store
if (!Arrays.equals(firstTokenID, TRX_SYMBOL_BYTES)) {
  String firstTokenRealID = assetIssueStore.get(firstTokenID).getId();
  firstTokenID = firstTokenRealID.getBytes();
}
if (!Arrays.equals(secondTokenID, TRX_SYMBOL_BYTES)) {
  String secondTokenRealID = assetIssueStore.get(secondTokenID).getId();
  secondTokenID = secondTokenRealID.getBytes();
}
```
`AssetIssueStore.get()` explicitly returns `null` for a missing key: [2](#0-1) 
```
@Override
public AssetIssueCapsule get(byte[] key) {
  return super.getUnchecked(key);
}
```
`doValidate()` in the same actuator never checks `assetIssueStore.get(firstTokenID/secondTokenID) != null` (contrast with `ParticipateAssetIssueActuator`, which does check `assetIssueCapsule == null` before use). It only checks that the *caller's account* has enough of the asset via `accountCapsule.assetBalanceEnoughV2(...)`, which inspects the account's own asset-balance map, not the existence of a corresponding entry keyed by name in the legacy `AssetIssueStore`: [3](#0-2) 

Because tokens issued while `AllowSameTokenName == 1` are written **only** to `AssetIssueV2Store` (see `AssetIssueActuator.execute()`, which only writes both stores when `getAllowSameTokenName() == 0`): [4](#0-3) 
an account can legitimately hold an asset balance for a token name that has no entry in the legacy `AssetIssueStore`. If `ExchangeCreateActuator` later runs its `getAllowSameTokenName() == 0` branch (default state on any freshly-deployed/private chain before the `AllowSameTokenName` proposal is ever passed, or after it is toggled back off via committee proposal) and the caller references such a token as `firstTokenId`/`secondTokenId`, `assetIssueStore.get(...)` returns `null` and `.getId()` throws an unchecked `NullPointerException` inside `execute()`.

### Impact Explanation
The `NullPointerException` is not one of the caught exception types in `execute()`'s catch clause (`BalanceInsufficientException | InvalidProtocolBufferException | ArithmeticException`), so it propagates uncaught out of the actuator. This occurs during `Manager.processTransaction()` / `Manager.processBlock()`, i.e. inside consensus-critical block application. An uncaught `RuntimeException` here is not part of the actuator's documented failure contract (unlike `ContractValidateException`/`ContractExeException`, which are gracefully converted into failed-transaction receipts), so it can abort block processing for any node applying this transaction — a denial-of-service against transaction/block processing (crash or halted block application) triggered by a single unprivileged, signed transaction, matching the "node crash or halt" acceptance criterion.

### Likelihood Explanation
Exploitability depends on `DynamicPropertiesStore.getAllowSameTokenName() == 0`, which is the default state of any freshly bootstrapped java-tron network (private chains, sidechains, test networks) before the corresponding committee proposal is ever passed, and is also technically re-settable by committee proposal. On such a network, any account holding an asset balance whose token was only ever recorded in `AssetIssueV2Store` (e.g., created transiently under `AllowSameTokenName == 1`, or via any code path that populates the V2 store without a matching legacy entry) can trigger the NPE with a single `ExchangeCreateContract` broadcast — no special privilege required.

### Recommendation
Add explicit null checks for `assetIssueStore.get(firstTokenID)` / `assetIssueStore.get(secondTokenID)` in `ExchangeCreateActuator.execute()` (and mirror the check in `doValidate()`, following the pattern used in `ParticipateAssetIssueActuator`), throwing `ContractExeException`/`ContractValidateException` instead of allowing an NPE, and ensure `execute()`'s catch clause also handles unexpected `RuntimeException`s defensively.

### Proof of Concept
1. Deploy/operate a java-tron network with `AllowSameTokenName` at its default value of `0`.
2. Get the account's asset balance map to contain an entry for a token name/ID that is present only in `AssetIssueV2Store` and absent from the legacy `AssetIssueStore` (achievable through any sequence of asset issuance/committee-proposal toggling that leaves the legacy store out of sync, e.g. issuing while `AllowSameTokenName==1` then having it revert to `0`).
3. Broadcast an `ExchangeCreateContract` transaction using that token's name as `first_token_id` (or `second_token_id`) together with a second valid token (or TRX), with sufficient balances to pass `doValidate()`.
4. During `execute()`, `assetIssueStore.get(firstTokenID)` returns `null`; `.getId()` throws `NullPointerException`, propagating uncaught through `Manager.processTransaction()`/`processBlock()`. [1](#0-0) [2](#0-1) [4](#0-3)

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeCreateActuator.java (L93-101)
```java
        //save to new asset store
        if (!Arrays.equals(firstTokenID, TRX_SYMBOL_BYTES)) {
          String firstTokenRealID = assetIssueStore.get(firstTokenID).getId();
          firstTokenID = firstTokenRealID.getBytes();
        }
        if (!Arrays.equals(secondTokenID, TRX_SYMBOL_BYTES)) {
          String secondTokenRealID = assetIssueStore.get(secondTokenID).getId();
          secondTokenID = secondTokenRealID.getBytes();
        }
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeCreateActuator.java (L210-228)
```java
    if (Arrays.equals(firstTokenID, TRX_SYMBOL_BYTES)) {
      if (accountCapsule.getBalance() < addExact(firstTokenBalance, calcFee())) {
        throw new ContractValidateException("balance is not enough");
      }
    } else {
      if (!accountCapsule.assetBalanceEnoughV2(firstTokenID, firstTokenBalance, dynamicStore)) {
        throw new ContractValidateException("first token balance is not enough");
      }
    }

    if (Arrays.equals(secondTokenID, TRX_SYMBOL_BYTES)) {
      if (accountCapsule.getBalance() < addExact(secondTokenBalance, calcFee())) {
        throw new ContractValidateException("balance is not enough");
      }
    } else {
      if (!accountCapsule.assetBalanceEnoughV2(secondTokenID, secondTokenBalance, dynamicStore)) {
        throw new ContractValidateException("second token balance is not enough");
      }
    }
```

**File:** chainbase/src/main/java/org/tron/core/store/AssetIssueStore.java (L26-29)
```java
  @Override
  public AssetIssueCapsule get(byte[] key) {
    return super.getUnchecked(key);
  }
```

**File:** actuator/src/main/java/org/tron/core/actuator/AssetIssueActuator.java (L78-87)
```java
      if (dynamicStore.getAllowSameTokenName() == 0) {
        assetIssueCapsuleV2.setPrecision(0);
        assetIssueStore
            .put(assetIssueCapsule.createDbKey(), assetIssueCapsule);
        assetIssueV2Store
            .put(assetIssueCapsuleV2.createDbV2Key(), assetIssueCapsuleV2);
      } else {
        assetIssueV2Store
            .put(assetIssueCapsuleV2.createDbV2Key(), assetIssueCapsuleV2);
      }
```
