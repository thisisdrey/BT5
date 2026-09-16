### Title
Missing bounds check on `Contract.getTypeValue()` before indexing fixed-size permission `operations` bitmap causes uncaught `IndexOutOfBoundsException` - (File: `actuator/src/main/java/org/tron/core/utils/TransactionUtil.java`)

### Summary
`checkPermissionOperations()` uses the raw protobuf enum wire value of a transaction's `Contract.type` field (`getTypeValue()`) to compute a byte offset into a fixed 32-byte (256-bit) `Permission.operations` bitmap, with no check that the value is within `[0, 255]`. This mirrors the CVE-2026-68128 root cause: an attacker-controlled index is used to address a fixed-size bitmap without a range check before the bitwise operation, leading to an out-of-bounds access.

### Finding Description
`TransactionUtil.checkPermissionOperations()` computes: [1](#0-0) 

```
int contractType = contract.getTypeValue();
boolean b = (operations.byteAt(contractType / 8) & (1 << (contractType % 8))) != 0;
```

`operations` is validated only for `size() != 32` immediately above, but `contractType` itself is never range-checked. `contract.getTypeValue()` returns the *raw int32* stored on the wire for the `ContractType type = 1;` enum field, which is independent from the safe, closed accessor `contract.getType()` (which maps unknown values to `UNRECOGNIZED`). Protobuf's varint wire encoding for an enum field accepts any int32 (including negative numbers and very large values); `getTypeValue()` surfaces that raw value unmodified. There is no upper- or lower-bound check anywhere before this method reads the byte at `contractType / 8`, so a `contractType` outside `[0, 255]` produces an index outside `[0, 31]`, and `ByteString.byteAt()` throws `IndexOutOfBoundsException` for an out-of-range index.

The identical unguarded pattern also exists in:
- `WalletUtil.checkPermissionOperations()` [2](#0-1) 
- `TransactionCapsule.checkPermission()`, which is invoked from the transaction multi-sig verification path used when a permission id other than `0` is used [3](#0-2) 

Critically, `TransactionCapsule.validatePubSignature()`/`validateSignature()` only catches `SignatureException | PermissionException | SignatureFormatException`; a `RuntimeException` such as `IndexOutOfBoundsException` raised inside the permission-operation check is not caught there: [4](#0-3) 

This signature/permission validation runs on every incoming transaction that uses a non-zero `permissionId` (multi-sig), which is a normal, unprivileged feature (`AccountPermissionUpdateContract` lets any account owner configure Active permissions). An attacker fully controls both the account's own permission configuration and the raw bytes of the `Contract.type` field in a self-crafted transaction they broadcast.

### Impact Explanation
An unhandled `IndexOutOfBoundsException` thrown deep in transaction/signature validation, outside the exception types the caller expects, propagates as an uncaught runtime exception during transaction processing (mempool acceptance / block application). This can crash or destabilize node processing of that transaction, i.e., a denial-of-service against transaction/block processing reachable purely from a single unprivileged, self-signed transaction — matching the "node crash or halt" impact category.

### Likelihood Explanation
High reachability: any account can enable an Active permission (multi-sig) via `AccountPermissionUpdateContract`, then broadcast a transaction referencing that `permissionId` with a `Contract.type` field wire value forged to a large/negative int32 while leaving the actual `Any parameter` payload as a legitimate contract. No special privileges or waiting on other actors is required — a single crafted, self-signed transaction submitted through the normal broadcast/JSON-RPC/HTTP transaction paths is sufficient to reach `checkPermissionOperations()`.

### Recommendation
In `TransactionUtil.checkPermissionOperations()` (and the duplicated logic in `WalletUtil.java` and `TransactionCapsule.java`), validate `contractType` is within `[0, 255]` (i.e., `contractType >= 0 && contractType < operations.size() * 8`) before using it as a bit index, and throw a `PermissionException`/`ContractValidateException` for out-of-range values instead of allowing an unchecked `IndexOutOfBoundsException` to propagate.

### Proof of Concept
1. Attacker account A enables an Active permission with `threshold=1` via `AccountPermissionUpdateContract` (self-controlled, unprivileged).
2. Attacker crafts a `Transaction` whose `raw.contract[0].permission_id` is the newly created Active permission id (non-zero), and whose `raw.contract[0].type` field is serialized on the wire with an out-of-range varint value (e.g., `100000` or a negative encoding), while `parameter` still holds a normally-parseable contract `Any`.
3. Attacker signs and broadcasts this transaction (or calls `GetTransactionSignWeight`, which invokes the same `checkPermissionOperations()` code path via `TransactionUtil.getTransactionSignWeight`).
4. `checkPermission`/`checkPermissionOperations` computes `contractType / 8` far outside `[0, 31]`, and `operations.byteAt(...)` throws `IndexOutOfBoundsException`, which is not one of the caught exception types in `validateSignature`, propagating as an unhandled runtime error during transaction/signature validation.

### Citations

**File:** actuator/src/main/java/org/tron/core/utils/TransactionUtil.java (L171-180)
```java
  public static boolean checkPermissionOperations(Permission permission, Contract contract)
      throws PermissionException {
    ByteString operations = permission.getOperations();
    if (operations.size() != 32) {
      throw new PermissionException("operations size must be 32");
    }
    int contractType = contract.getTypeValue();
    boolean b = (operations.byteAt(contractType / 8) & (1 << (contractType % 8))) != 0;
    return b;
  }
```

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
