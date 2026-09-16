### Title
Unchecked `IndexOutOfBoundsException` in permission-operation bitmap check crashes transaction validation for a crafted `permissionId`/contract-type combination - (File: `chainbase/src/main/java/org/tron/common/utils/WalletUtil.java`)

### Summary
Similar to CVE-2021-38198, where KVM incorrectly computed a shadow-page access-permission bitmask and thereby skipped a protection check, java-tron's active-permission operation bitmap check computes a bit index from an untrusted, transaction-supplied value without validating that the derived index is in range of the fixed 32-byte `operations` bitmap before indexing into it.

### Finding Description
`WalletUtil.checkPermissionOperations()` and its duplicate in `actuator/.../TransactionUtil.java` compute a bit position directly from `contract.getTypeValue()`: [1](#0-0) 

`contractType / 8` is used as a byte index into `operations`, a fixed 32-byte `ByteString` (256 bits), with no bounds check that `contractType` is within `[0, 255]`. `Transaction.Contract.type` is a protobuf enum field; `getTypeValue()` returns the *raw wire integer* for the field even when it doesn't correspond to a defined `ContractType` enum constant (an "unrecognized"/out-of-range value), because protobuf enum fields accept any 32-bit int on the wire. An attacker fully controls the raw bytes of a broadcast transaction's `Contract.type` field, so they can set an arbitrary 32-bit integer (e.g., far greater than 255, or negative) that decodes successfully at the protobuf layer but produces an out-of-range or negative index (`contractType/8`, `contractType%8`) when passed into `checkPermissionOperations`.

This method is invoked from the core signature/permission verification path used for every incoming transaction that uses a non-default (`permissionId != 0`) multisig "Active" permission: [2](#0-1) 
which is called from `validateSignature`/`addSign`: [3](#0-2) 
and from `Wallet.getTransactionApprovedList`/`TransactionUtil.getTransactionSignWeight`, both reachable via public gRPC/HTTP API calls: [4](#0-3) 

`ByteString.byteAt(index)` throws `ArrayIndexOutOfBoundsException`/`IndexOutOfBoundsException` for an out-of-range index. This exception type is a `RuntimeException`, not one of `SignatureException | PermissionException | SignatureFormatException`, which are the only exceptions explicitly caught around `validateSignature` in `validatePubSignature`: [5](#0-4) 
so it is not converted into a normal `ValidateSignatureException` and instead propagates as an unexpected runtime exception out of the transaction-validation call stack.

### Impact Explanation
An unprivileged transaction broadcaster who has (or creates) an account with a non-default Active permission (`permissionId >= 2`, obtainable by any account via `AccountPermissionUpdateContract`, which any account can send to itself) can subsequently submit a transaction whose `Contract.type` raw value is an out-of-range integer. When that transaction reaches signature/permission validation (`TransactionCapsule.validateSignature`/`addSign`, or the `getTransactionApprovedList`/`getTransactionSignWeight` query APIs), the bit-index computation reads outside the fixed 32-byte `operations` array, throwing an uncaught `IndexOutOfBoundsException`. Depending on where this propagates (broadcast path vs. block-application/re-verification path in `Manager`), this can result in rejection failures being mishandled, unexpected node behavior, or in the worst case an uncaught runtime exception during the transaction re-verification pipeline used when packing/replaying transactions — a denial-of-service condition against transaction processing. This matches the "node crash or halt"/"API the node can no longer serve" impact classes.

### Likelihood Explanation
Likelihood is limited by two factors I could not fully confirm within the exploration budget: (1) whether protobuf-java's generated code for `Transaction.Contract` truly preserves an arbitrary raw int for `getTypeValue()` when the wire value doesn't match a `ContractType` enum constant (this is standard proto3 open-enum behavior, but I did not locate the generated `Contract` builder source to confirm no additional validation is layered on deserialization), and (2) whether every call path that reaches `checkPermissionOperations` is wrapped in a broader `try { } catch (Exception e)` at a level above `validatePubSignature`/`addSign` (e.g., in `Manager.pushTransaction` or `Wallet.broadcastTransaction`), which would downgrade the impact from a crash to a normal rejected-transaction response. I was not able to view `Manager.java`'s contents (the tool returned only the package line, suggesting an indexing limitation) to verify catch coverage of `pushTransaction`/block re-verification for this specific exception type.

### Recommendation
Add explicit bounds validation before indexing into `operations` in both `WalletUtil.checkPermissionOperations` and `TransactionUtil.checkPermissionOperations`: reject (with a `PermissionException`) any `contractType` value outside `[0, 255]` (i.e., `operations.size() * 8`) before computing `contractType / 8` / `contractType % 8`, and ensure the resulting `IndexOutOfBoundsException` class of errors cannot escape as unchecked runtime exceptions from the signature-verification code path.

### Proof of Concept
Due to indexing limitations I could not access the generated protobuf `Contract` class or `Manager.java` contents to build and confirm a concrete byte-level transaction payload and its exact propagation/crash behavior at runtime. I recommend that a Devin session with full repository and build access construct: (1) an account with an `Active` permission (`permissionId=2`) via `AccountPermissionUpdateContract`, (2) a `Transaction.Contract` whose serialized `type` field is set to a raw varint value greater than 255 (bypassing the Java enum setter by writing raw bytes with a proto reflection/dynamic-message helper), (3) a `permissionId` of `2` referencing that Active permission, and then invoke `Wallet.getTransactionApprovedList` or the broadcast path to confirm whether an uncaught `IndexOutOfBoundsException` results, and trace whether it is caught before or after the block-application/re-verification pipeline in `Manager`.

### Citations

**File:** chainbase/src/main/java/org/tron/common/utils/WalletUtil.java (L27-37)
```java
  public static boolean checkPermissionOperations(Permission permission, Contract contract)
      throws PermissionException {
    ByteString operations = permission.getOperations();
    if (operations.size() != 32) {
      throw new PermissionException(String.format("operations size must 32, actual: %d",
          operations.size()));
    }
    int contractType = contract.getTypeValue();
    boolean b = (operations.byteAt(contractType / 8) & (1 << (contractType % 8))) != 0;
    return b;
  }
```

**File:** chainbase/src/main/java/org/tron/core/capsule/TransactionCapsule.java (L468-491)
```java
  public static boolean validateSignature(Transaction transaction,
      byte[] hash, AccountStore accountStore, DynamicPropertiesStore dynamicPropertiesStore)
      throws PermissionException, SignatureException, SignatureFormatException {
    Transaction.Contract contract = transaction.getRawData().getContractList().get(0);
    int permissionId = contract.getPermissionId();
    byte[] owner = getOwner(contract);
    AccountCapsule account = accountStore.get(owner);
    Permission permission = null;
    if (account == null) {
      if (permissionId == 0) {
        permission = AccountCapsule.getDefaultPermission(ByteString.copyFrom(owner));
      }
      if (permissionId == 2) {
        permission = AccountCapsule
            .createDefaultActivePermission(ByteString.copyFrom(owner), dynamicPropertiesStore);
      }
    } else {
      permission = account.getPermissionById(permissionId);
    }
    if (permission == null) {
      throw new PermissionException("permission isn't exit");
    }
    checkPermission(permissionId, permission, contract);
    long weight = checkWeight(permission, transaction.getSignatureList(), hash, null);
```

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

**File:** framework/src/main/java/org/tron/core/Wallet.java (L658-678)
```java
      try {
        Contract contract = trx.getRawData().getContract(0);
        byte[] owner = TransactionCapsule.getOwner(contract);
        AccountCapsule account = chainBaseManager.getAccountStore().get(owner);
        if (account == null) {
          throw new PermissionException("Account does not exist!");
        }
        int permissionId = contract.getPermissionId();
        Permission permission = account.getPermissionById(permissionId);
        if (permission == null) {
          throw new PermissionException("Permission for this, does not exist!");
        }
        if (permissionId != 0) {
          if (permission.getType() != PermissionType.Active) {
            throw new PermissionException("Permission type is wrong!");
          }
          //check operations
          if (!WalletUtil.checkPermissionOperations(permission, contract)) {
            throw new PermissionException("Permission denied!");
          }
        }
```
