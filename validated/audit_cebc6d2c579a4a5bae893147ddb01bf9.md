### Title
Unvalidated contract-type value used to index a fixed 32-byte permission `operations` bitmap - ([File: chainbase/src/main/java/org/tron/common/utils/WalletUtil.java])

### Summary
`WalletUtil.checkPermissionOperations` and its duplicate `TransactionUtil.checkPermissionOperations` compute an index into a fixed-size 32-byte (256-bit) `Permission.operations` bitmap directly from `contract.getTypeValue()` without validating that the value is in the expected `ContractType` range, mirroring the hns3 pattern of using an attacker-influenced id to index a fixed-size bitmap without bounds checking.

### Finding Description
`Permission.operations` is documented and enforced to be exactly 32 bytes ("1 bit 1 contract") [1](#0-0) . The lookup helper reads the raw enum wire value and uses it as a bit index with no upper-bound check: [2](#0-1) 

The identical unchecked pattern also exists in `TransactionUtil.checkPermissionOperations`: [3](#0-2) 

`contract.getTypeValue()` returns the raw `int32` stored on the wire for the `Contract.type` field of `Transaction.Contract`. Because protobuf3 enum fields accept unrecognized values on the wire (`getType()` would return `UNRECOGNIZED`, but `getTypeValue()` returns the actual raw int, which can be any value up to `Integer.MAX_VALUE` or even negative depending on encoding), an attacker who crafts a raw transaction with an out-of-declared-range contract type value fully controls `contractType`. `contractType / 8` and `contractType % 8` are then used to call `ByteString.byteAt(index)` on a 32-byte array without checking `index < 32`.

This code path is reached during transaction permission checks, which run before any contract-type-specific actuator validation: `TransactionCapsule.checkPermission` (called from `validateSignature`, called from `validatePubSignature`, called on every signature check for a transaction using a non-owner/non-zero `permissionId`) invokes `checkPermissionOperations`: [4](#0-3) [5](#0-4) 

It is also reachable from the gRPC/HTTP query path `Wallet.getTransactionApprovedList`, which accepts an arbitrary (even unsigned) transaction from any client and runs the same permission check against the crafted `Contract.type`: [6](#0-5) 

### Impact Explanation
Unlike the original C/kernel bug, Java's `ByteString.byteAt`/array access is bounds-checked, so this cannot cause raw out-of-bounds memory corruption; instead an out-of-range `contractType` (e.g., `contractType >= 256` or negative) causes `ByteString.byteAt` to throw an unchecked `IndexOutOfBoundsException`. `TransactionCapsule.validatePubSignature` only catches `SignatureException | PermissionException | SignatureFormatException`, not this runtime exception, so it will propagate uncaught out of the signature-verification path. Depending on the caller's exception handling, this can abort transaction processing unexpectedly (denial of service for that request/verification thread) rather than corrupt memory. I was unable to fully trace every downstream caller (e.g., `Manager.pushTransaction`/`TronNetDelegate`) in the time available to confirm whether a generic `catch (Exception e)` wraps every one of these call sites, so the exact blast radius (isolated request failure vs. broader processing disruption) is not fully verified from the code I could inspect.

### Likelihood Explanation
Any account holding (or claiming to hold, via `Wallet.getTransactionApprovedList`, which does not require the transaction to be signed) an Active permission can trigger this by setting `permissionId != 0` and crafting a `Transaction.Contract` whose `type` field wire value is outside `[0,255]`. This is reachable from a single unauthenticated/unsigned API request or a permissioned account's normal transaction submission, requiring no privileged access.

### Recommendation
Validate `contract.getTypeValue()` (or equivalently `contractType`) is within `[0, operations.size() * 8)` before computing `contractType / 8` / `contractType % 8` in both `WalletUtil.checkPermissionOperations` and `TransactionUtil.checkPermissionOperations`, returning `false`/throwing a `PermissionException` for out-of-range values instead of indexing directly.

### Proof of Concept
1. Craft a raw `Transaction` whose single `Contract` has `permission_id = 2` (non-owner Active permission) and a `type` field wire value larger than 255 (or negative), bypassing the strongly-typed `ContractType` enum by writing the varint directly at the protobuf wire level.
2. Submit this transaction (or an unsigned copy) to `Wallet.getTransactionApprovedList` via the exposed gRPC/HTTP API, or attempt to sign/verify it through `TransactionCapsule.validatePubSignature`.
3. `checkPermissionOperations` computes `operations.byteAt(contractType / 8)` on the account's 32-byte `operations` bitmap; with `contractType >= 256` this throws `IndexOutOfBoundsException`, which is not declared/caught in `validatePubSignature`'s catch clause, causing the verification call to fail with an unexpected runtime exception instead of a controlled `PermissionException`.

### Citations

**File:** protocol/src/main/protos/core/Tron.proto (L261-274)
```text
message Permission {
  enum PermissionType {
    Owner = 0;
    Witness = 1;
    Active = 2;
  }
  PermissionType type = 1;
  int32 id = 2; //Owner id=0, Witness id=1, Active id start by 2
  string permission_name = 3;
  int64 threshold = 4;
  int32 parent_id = 5;
  bytes operations = 6; //1 bit 1 contract
  repeated Key keys = 7;
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

**File:** framework/src/main/java/org/tron/core/Wallet.java (L659-678)
```java
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
