### Title
Global out-of-bounds-style crash via unchecked `ContractType` raw value used as bit-index into fixed 32-byte `operations` bitmap - ([File: chainbase/src/main/java/org/tron/common/utils/WalletUtil.java])

### Summary
`WalletUtil.checkPermissionOperations` (and its duplicate in `actuator/src/main/java/org/tron/core/utils/TransactionUtil.java`) computes a bit-index into a fixed-size 32-byte `operations` bitmap using `contract.getTypeValue()` without validating that the value is within the expected `[0,255]` range backed by the actual `ContractType` enum. This mirrors the rmnet_policy bug class: an attacker-controlled "type" value is used directly as an array index against a fixed-size table without checking it against the true bound of that table.

### Finding Description
`Permission.operations` is documented and enforced to be exactly 32 bytes (256 bits, "1 bit 1 contract") — see the proto comment `bytes operations = 6; //1 bit 1 contract` [1](#0-0) . The size check only validates the container size, not the index used to read from it:

```java
public static boolean checkPermissionOperations(Permission permission, Contract contract)
    throws PermissionException {
  ByteString operations = permission.getOperations();
  if (operations.size() != 32) {
    throw new PermissionException(...);
  }
  int contractType = contract.getTypeValue();
  boolean b = (operations.byteAt(contractType / 8) & (1 << (contractType % 8))) != 0;
  return b;
}
``` [2](#0-1) 

`contract.getTypeValue()` returns the *raw* protobuf int32 stored on the wire for the `ContractType type = 1;` enum field [3](#0-2) . Protobuf enum wire values are **not restricted** to the enum's declared constants (0–59 in `ContractType`) — the generated Java `getType()` accessor maps unrecognized values to `UNRECOGNIZED`, but `getTypeValue()` still returns the arbitrary raw int, including negative numbers or values far outside `[0,255]`. Nothing upstream of `checkPermissionOperations` restricts `contract.getTypeValue()` before this call — the `Contract.type` field is populated directly from transaction bytes supplied by the broadcaster.

Consequently, `contractType/8` can be negative or ≥32, causing `operations.byteAt(idx)` to be called with an index outside the valid `[0,31]` range of the 32-byte `ByteString`, which throws `ArrayIndexOutOfBoundsException`/`IndexOutOfBoundsException` — the Java analogue of the kernel's OOB read: an unchecked, attacker-influenced value used as an index into a statically-sized backing store.

This exact same unchecked-index pattern is duplicated in `TransactionUtil.checkPermissionOperations` [4](#0-3) .

Reachability: this function is invoked from `TransactionCapsule.checkPermission`, which is called by `TransactionCapsule.validateSignature(Transaction, ...)` whenever a contract uses `permissionId != 0` (multisig/active permission), i.e., on every ordinary signature validation of a broadcast transaction:

```java
private static void checkPermission(int permissionId, Permission permission, Transaction.Contract contract) throws PermissionException {
  if (permissionId != 0) {
    if (permission.getType() != PermissionType.Active) {
      throw new PermissionException("Permission type is error");
    }
    if (!checkPermissionOperations(permission, contract)) {
      throw new PermissionException("Permission denied");
    }
  }
}
``` [5](#0-4) 

This is invoked from `TransactionCapsule.validatePubSignature` → `validateSignature`, which is called from `Manager.processTransaction` for every transaction pushed into a block or the pending pool [6](#0-5) , and by `Manager.processBlock` for every transaction contained in an incoming block [7](#0-6) . The `PermissionException` thrown by well-formed cases is caught in `validatePubSignature`'s try/catch, but that catch only lists `SignatureException | PermissionException | SignatureFormatException` — it does **not** catch the `ArrayIndexOutOfBoundsException` that `byteAt` throws for out-of-range indices [8](#0-7) . The same unguarded pattern is exercised in `Wallet.getTransactionApprovedList`/`TransactionUtil.getTransactionSignWeight` (query paths), where it is caught by a generic `catch (Exception ex)`, but in the core `processTransaction`/`processBlock` path it is not shielded, so it can propagate as an unhandled runtime exception during block application.

### Impact Explanation
An attacker who controls an account with any active/multisig `Permission` (which any account owner can set up themselves via `AccountPermissionUpdateContract`, or an attacker crafting a raw transaction where `Contract.type` is manually forged with an out-of-range int32 while `permissionId != 0`) can trigger an unhandled `IndexOutOfBoundsException` during transaction/block processing in `Manager`. If this happens while processing a block (`processBlock`) rather than the pooled-transaction gRPC path (which is protected by a broad `catch (Exception e)` in `Wallet.broadcastTransaction`), the exception is not one of the checked exceptions declared/caught along that path, and would propagate as an uncaught `RuntimeException`, potentially aborting block application and halting/crashing the node process — matching the "node crash or halt" impact bucket.

### Likelihood Explanation
Triggering requires a raw/forged transaction where the `Contract.type` protobuf field carries a raw int value outside `[0,255]` (easy to construct by any party crafting bytes directly rather than via the standard SDK builders, since protobuf places no validation on enum wire values), together with a `permissionId != 0` referencing an `Active` permission on the sender account (which the sender fully controls). No special privilege beyond normal account/permission setup and the ability to submit an arbitrary transaction is needed, making this reachable by any unprivileged transaction broadcaster.

### Recommendation
Bound-check `contractType` before indexing: reject or treat as "false" any `contractType` outside `[0, 255]` (i.e., `contractType < 0 || contractType >= 256`) in both `WalletUtil.checkPermissionOperations` and `TransactionUtil.checkPermissionOperations`, mirroring the correct fix pattern in the CVE (use the real bound of the backing array instead of trusting the raw enum wire value). Also verify `contract.getType() != UNRECOGNIZED` before relying on `getTypeValue()`.

### Proof of Concept
1. Craft an account with an `Active` permission (`permissionId = 2`, threshold 1, one key) and non-empty `operations` bitmap (32 bytes).
2. Construct a `Transaction` whose single `Contract` has `permission_id = 2` and whose `type` field is serialized on the wire with a raw varint value far outside the declared `ContractType` enum range (e.g., `100000` or a negative-producing value), while the `parameter` payload still deserializes as a contract compatible enough to pass earlier checks.
3. Submit/replay this transaction so that `TransactionCapsule.validateSignature` → `checkPermission` → `checkPermissionOperations` is invoked with `contract.getTypeValue()` equal to the forged out-of-range value.
4. `operations.byteAt(contractType / 8)` throws `ArrayIndexOutOfBoundsException` because `contractType / 8` is outside `[0,31]`; this exception is not caught by the `SignatureException | PermissionException | SignatureFormatException` catch clause in `validatePubSignature`, propagating up through `processTransaction`/`processBlock` in `Manager`.

### Citations

**File:** protocol/src/main/protos/core/Tron.proto (L272-272)
```text
  bytes operations = 6; //1 bit 1 contract
```

**File:** protocol/src/main/protos/core/Tron.proto (L380-380)
```text
    ContractType type = 1;
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

**File:** chainbase/src/main/java/org/tron/core/capsule/TransactionCapsule.java (L666-676)
```java
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
```

**File:** framework/src/main/java/org/tron/core/db/Manager.java (L1542-1546)
```java
    if (!trxCap.validateSignature(chainBaseManager.getAccountStore(),
        chainBaseManager.getDynamicPropertiesStore())) {
      throw new ValidateSignatureException(
          String.format(" %s transaction signature validate failed", txId));
    }
```

**File:** framework/src/main/java/org/tron/core/db/Manager.java (L1884-1897)
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
```
