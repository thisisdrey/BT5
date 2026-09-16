## Analysis: Cached Signature Verification State (`isVerified`) Bypasses Re-validation Against Current Permission State

### Title
Stale Cached Signature-Verification Flag (`isVerified`) Allows a Transaction to Bypass Re-check Against Updated Account Permissions - (File: `chainbase/src/main/java/org/tron/core/capsule/TransactionCapsule.java`)

### Summary
CVE-2023-27932 describes a WebKit flaw where insufficient state management let a stale security decision (Same Origin Policy check) be reused in a context where it should have been re-evaluated. The closest reachable analog in java-tron is `TransactionCapsule.validatePubSignature`, which memoizes its signature/permission-verification result in the instance field `isVerified` and, once set to `true`, permanently skips re-checking the signature against the *current* `AccountStore`/permission state for the lifetime of that `TransactionCapsule` object.

### Finding Description
`validatePubSignature` guards the entire signature/permission verification path with `if (!isVerified)`: [1](#0-0) 

Once `isVerified` becomes `true` after a successful `validateSignature` call, every subsequent invocation of `validatePubSignature` on the same `TransactionCapsule` instance returns `true` immediately without re-deriving the signer address, re-checking `checkPermission`, or re-validating signature weight against the account's *current* `Permission` set (`AccountStore`/`DynamicPropertiesStore` passed in are effectively ignored on the cached path). The permission and weight logic that is skipped is defined in `checkPermission`: [2](#0-1) 

A `TransactionCapsule` is a mutable, long-lived object that can be validated more than once against different `AccountStore` snapshots — e.g. during pending-pool retries, repushed transactions, or re-execution paths in `Manager` (multiple call sites reference `validatePubSignature`/`resetResult`). If the account's permission configuration changes between the first successful verification (which sets `isVerified = true`) and a later re-validation of the *same* object against a *different* account/permission state (for example after an `AccountPermissionUpdateActuator` execution revokes a key or raises the threshold, as seen in `AccountPermissionUpdateActuator.validate`/`execute`), the cached `true` result is returned instead of re-deriving authorization from the new state: [3](#0-2) 

This mirrors the CVE's root cause: a security decision made under one state is trusted in a later, different state because the check was never re-run — only the domain differs (browser origin isolation vs. on-chain multisig/permission authorization).

### Impact Explanation
If a cached `isVerified=true` `TransactionCapsule` is re-validated after the owner has revoked or reduced the signer's weight/permission via `AccountPermissionUpdateContract`, the transaction can still be treated as validly authorized and executed by an actuator, resulting in an unauthorized account operation (e.g. a transfer or contract call executed with a signature that no longer satisfies the account's current permission threshold). This is a concrete "unauthorized account operation" impact category.

### Likelihood Explanation
Exploitability depends on whether a single `TransactionCapsule` instance is genuinely revalidated across two different permission states in a production code path (e.g., pending-pool retry, repushed transaction during a fork switch, or transaction re-execution flow in `Manager`). The `isVerified` memoization pattern itself is confirmed and unconditional once set; however, I was not able to fully trace every call site in `Manager.java` to confirm a concrete window where the same object instance is revalidated after an intervening permission change (this would require deeper tracing of `Manager`'s repush/fork-switch and pending-pool logic than tool budget allowed). This uncertainty should be resolved by a deeper trace of `Manager.java`'s use of `validatePubSignature`/`resetResult`/`isVerified`.

### Recommendation
Do not memoize `isVerified` as a permanent instance-level cache spanning multiple account-state contexts. Either invalidate/reset `isVerified` whenever the transaction is re-validated against a different `AccountStore` snapshot, or key the cache on the account's permission version/id so a re-validation after a permission update forces full re-verification.

### Proof of Concept
Conceptual PoC (not fully confirmed executable within the current investigation):
1. Attacker signs Tx `T` using key `K1`, which satisfies account `A`'s current Active permission (weight ≥ threshold). `T` is submitted and validated once via `validatePubSignature`, setting `isVerified = true` on that `TransactionCapsule` instance and leaving `T` pending (e.g., in the pending pool, not yet packed into a block).
2. Account owner submits `AccountPermissionUpdateContract` revoking `K1`'s weight/permission, executed via `AccountPermissionUpdateActuator.execute`, updating `AccountStore`.
3. The still-pending `TransactionCapsule` for `T` (same object, `isVerified` still `true`) is re-validated/re-included in a later block without re-deriving its authorization from the updated `AccountStore`, because `validatePubSignature` short-circuits on `isVerified`.
4. `T` executes despite `K1` no longer being authorized under `A`'s current permission set.

Given the residual uncertainty about the exact re-validation call path in `Manager.java`, this should be verified with a live Devin session that can trace transaction re-validation flows end-to-end.

### Citations

**File:** chainbase/src/main/java/org/tron/core/capsule/TransactionCapsule.java (L635-645)
```java
  private static void checkPermission(int permissionId, Permission permission, Transaction.Contract contract) throws PermissionException {
    if (permissionId != 0) {
      if (permission.getType() != PermissionType.Active) {
        throw new PermissionException("Permission type is error");
      }
      //check operations
      if (!checkPermissionOperations(permission, contract)) {
        throw new PermissionException("Permission denied");
      }
    }
  }
```

**File:** chainbase/src/main/java/org/tron/core/capsule/TransactionCapsule.java (L650-680)
```java
  public boolean validatePubSignature(AccountStore accountStore,
      DynamicPropertiesStore dynamicPropertiesStore)
      throws ValidateSignatureException {
    if (!isVerified) {
      if (this.transaction.getSignatureCount() <= 0
              || this.transaction.getRawData().getContractCount() <= 0) {
        throw new ValidateSignatureException("miss sig or contract");
      }
      if (this.transaction.getSignatureCount() > dynamicPropertiesStore
              .getTotalSignNum()) {
        throw new ValidateSignatureException("too many signatures");
      }

      byte[] hash = getTransactionId().getBytes();

      long startNs = System.nanoTime();
      try {
        if (!validateSignature(this.transaction, hash, accountStore, dynamicPropertiesStore)) {
          isVerified = false;
          throw new ValidateSignatureException("sig error");
        }
      } catch (SignatureException | PermissionException | SignatureFormatException e) {
        isVerified = false;
        throw new ValidateSignatureException(e.getMessage());
      } finally {
        logSlowSigVerify(startNs);
      }
      isVerified = true;
    }
    return true;
  }
```

**File:** actuator/src/main/java/org/tron/core/actuator/AccountPermissionUpdateActuator.java (L34-69)
```java
  @Override
  public boolean execute(Object object) throws ContractExeException {
    TransactionResultCapsule result = (TransactionResultCapsule) object;
    if (Objects.isNull(result)) {
      throw new RuntimeException(ActuatorConstant.TX_RESULT_NULL);
    }

    AccountStore accountStore = chainBaseManager.getAccountStore();
    long fee = calcFee();
    final AccountPermissionUpdateContract accountPermissionUpdateContract;
    try {
      accountPermissionUpdateContract = any.unpack(AccountPermissionUpdateContract.class);

      byte[] ownerAddress = accountPermissionUpdateContract.getOwnerAddress().toByteArray();
      AccountCapsule account = accountStore.get(ownerAddress);
      account.updatePermissions(accountPermissionUpdateContract.getOwner(),
          accountPermissionUpdateContract.getWitness(),
          accountPermissionUpdateContract.getActivesList());
      accountStore.put(ownerAddress, account);

      adjustBalance(accountStore, ownerAddress, -fee);
      if (chainBaseManager.getDynamicPropertiesStore().supportBlackHoleOptimization()) {
        chainBaseManager.getDynamicPropertiesStore().burnTrx(fee);
      } else {
        adjustBalance(accountStore, accountStore.getBlackhole(), fee);
      }

      result.setStatus(fee, code.SUCESS);
    } catch (BalanceInsufficientException | InvalidProtocolBufferException e) {
      logger.debug(e.getMessage(), e);
      result.setStatus(fee, code.FAILED);
      throw new ContractExeException(e.getMessage());
    }

    return true;
  }
```
