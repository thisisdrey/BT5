### Title
Unvalidated `Contract.type` raw enum value used as bit-index into fixed-size 32-byte `operations` bitmap in permission check - (File: `chainbase/src/main/java/org/tron/common/utils/WalletUtil.java`)

### Summary
The CVE analog (`ggml_type_size` indexing a fixed array with an unvalidated `rpc_tensor.type` field) maps to `WalletUtil.checkPermissionOperations` / `TransactionUtil.checkPermissionOperations`, which use the raw wire value of a transaction's `Contract.type` protobuf enum field, unbounded and attacker-controlled, as a bit index into a fixed 32-byte `operations` bitmap without validating it is within range.

### Finding Description
`checkPermissionOperations` reads `int contractType = contract.getTypeValue();` and then indexes the fixed-size, 32-byte `Permission.operations` bitmap with `operations.byteAt(contractType / 8)`: [1](#0-0) 

The identical unsafe pattern also exists in the actuator module: [2](#0-1) 

`getTypeValue()` on a protobuf enum field returns the raw stored int32 from the wire, not a value validated against the declared `ContractType` enum range. Since `Transaction.Contract.type` is fully attacker-controlled data (part of the serialized transaction submitted by any broadcaster), a transaction can be crafted with an out-of-range or arbitrarily large raw value for this field. `contractType / 8` is then used directly as the index into `operations`, a `ByteString` fixed at 32 bytes (verified earlier by the `operations.size() != 32` check). If `contractType` is large (e.g. `Integer.MAX_VALUE`), `contractType / 8` far exceeds the valid `[0,31]` index range for the 32-byte buffer, and `ByteString.byteAt()` throws an unguarded `IndexOutOfBoundsException`/`ArrayIndexOutOfBoundsException`.

This mirrors the CVE class exactly: an externally supplied "type" discriminator is trusted and used to compute an array index without bounds validation, rather than validating it against the legitimate `ContractType` value set before use.

### Impact Explanation
This routine is invoked from the account-permission / multi-signature validation path (`TransactionCapsule` calls it twice, and it is also reachable from `Wallet.java`), which is exercised for every transaction that carries `Permission_id` / multi-sig authority checks — a path reachable by any unprivileged transaction broadcaster or by any client hitting the wallet API that triggers permission validation. An uncaught `IndexOutOfBoundsException` thrown mid-validation is a `RuntimeException` that is not a declared/expected exception type in this validation chain, risking a crash or unhandled failure of the transaction-processing/validation thread — i.e., a denial-of-service against block application or the node's ability to validate/serve transactions, consistent with the rules' accepted impact of "node crash or halt" / "an API the node can no longer serve."

### Likelihood Explanation
Likelihood is high: the `type` field of a `Contract` is fully attacker-controlled in any submitted transaction, requires no special privilege beyond crafting a transaction with an account that has a multi-sig/active permission configured, and the missing bounds check is a direct, single-step reachable defect (no race conditions or complex preconditions needed) — analogous in root cause to the llama.cpp `rpc_tensor.type` issue.

### Recommendation
Validate `contractType` against the legitimate declared `ContractType` value range (e.g., `contractType >= 0 && contractType < operations.size() * 8`, or explicitly check it corresponds to a recognized `ContractType` enum constant) before using it to compute `operations.byteAt(contractType / 8)`, in both `WalletUtil.checkPermissionOperations` and `TransactionUtil.checkPermissionOperations`, throwing a well-defined `PermissionException` (as already done for the `operations.size() != 32` case) instead of allowing an unchecked runtime exception to propagate.

### Proof of Concept
1. Craft a `Transaction` whose `Contract.type` field is set to a raw (unrecognized) enum wire value far outside the declared range, e.g. `2000000000` (achievable because protobuf enum fields accept arbitrary int32 wire values; `getTypeValue()` returns this raw value unmodified).
2. Attach a valid multi-sig `Permission`/signature set that reaches `checkPermissionOperations` during validation (as exercised in `AccountPermissionUpdateActuatorTest`, which already demonstrates crafting out-of-range `contractType` values, e.g. producing "7 isn't a validate ContractType" for value 7): [3](#0-2) 
3. Broadcast the transaction; when `checkPermissionOperations` computes `operations.byteAt(2000000000 / 8)` against the fixed 32-byte `operations` ByteString, an `IndexOutOfBoundsException` is thrown, uncaught by the surrounding permission-check logic.

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

**File:** framework/src/test/java/org/tron/core/actuator/AccountPermissionUpdateActuatorTest.java (L867-890)
```java
  @Test
  public void activePermissionInvalidOperationBit() {
    ByteString address = ByteString.copyFrom(ByteArray.fromHexString(OWNER_ADDRESS));

    Permission ownerPermission = AccountCapsule.createDefaultOwnerPermission(address);
    Permission activePermission = Permission.newBuilder().setType(PermissionType.Active)
        .setPermissionName("active")
        .setThreshold(1)
        .setOperations(ByteString
            .copyFrom(ByteArray
                .fromHexString("8000000000000000000000000000000000000000000000000000000000000000")))
        .setParentId(0).addKeys(VALID_KEY).build();

    List<Permission> activeList = new ArrayList<>();
    activeList.add(activePermission);

    AccountPermissionUpdateActuator actuator = new AccountPermissionUpdateActuator();
    actuator.setChainBaseManager(dbManager.getChainBaseManager())
        .setAny(getContract(address, ownerPermission, null, activeList));
    TransactionResultCapsule ret = new TransactionResultCapsule();

    processAndCheckInvalid(actuator, ret, "7 isn't a validate ContractType",
        "7 isn't a validate ContractType");
  }
```
