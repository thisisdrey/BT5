### Title
Unchecked contract type value causes `IndexOutOfBoundsException` in multisig permission-operation bitmap check - ([File: chainbase/src/main/java/org/tron/common/utils/WalletUtil.java])

### Summary
`WalletUtil.checkPermissionOperations` and its actuator-side twin `TransactionUtil.checkPermissionOperations` derive an array index directly from the raw, attacker-controlled `Contract.getTypeValue()` field and use it to index into a fixed 32-byte `operations` bitmap without any range validation, mirroring the CVE-2021-47395 bug class (an unchecked/unvalidated value used to index a fixed-size structure, causing a crash).

### Finding Description
`checkPermissionOperations` reads the numeric contract-type value straight off the wire and uses it, unvalidated, to compute a byte offset and bit offset into a 32-byte permission bitmap: [1](#0-0) 

The same pattern exists in the actuator utility class used for transaction sign-weight/permission evaluation: [2](#0-1) 

`Contract.type` is declared as a protobuf `enum` field, but protobuf wire encoding for enum fields accepts arbitrary `int32` values, and `getTypeValue()` returns that raw integer regardless of whether it corresponds to a declared `ContractType` constant. The code only validates that `operations.size() == 32` (i.e., the *permission's* bitmap is 32 bytes), but never validates that `contractType / 8` stays within `[0, 31]`. Because `ByteString.byteAt(int)` throws `IndexOutOfBoundsException`/`StringIndexOutOfBoundsException` for any index outside the backing array, a transaction whose serialized `Contract.type` field carries a value such as `999999` (or a negative value, since `contractType / 8` and `contractType % 8` are computed with Java's truncating/negative-preserving integer division and modulo) will make `operations.byteAt(contractType / 8)` throw instead of returning a boolean.

This differs from the legitimate `WalletUtil.checkAvailableContractType`-style code elsewhere in the codebase, which iterates only over declared `ContractType.values()`; here the value comes directly from an externally supplied, unvalidated integer.

### Impact Explanation
Any code path that calls `checkPermissionOperations` on a permission-restricted (`permissionId != 0`) transaction whose `Contract.type` raw value is outside the valid `[0,255]`/`[0,31]*8` range will throw an uncaught `IndexOutOfBoundsException`. If this exception propagates through a call site that lacks a broad catch-all (unlike `TransactionUtil.getTransactionSignWeight`, which happens to catch generic `Exception`), it can abort transaction processing/validation with an unexpected runtime exception instead of a clean `PermissionException`, which is the correct, intended failure mode for malformed permission checks. Depending on the calling context (block application/transaction validation in `Manager`, or signature/permission verification during `pushTransaction`), an uncaught exception here can disrupt processing of that transaction path and represents unhandled-input crash behavior consistent with the CVE's "unchecked index causes crash" bug class.

### Likelihood Explanation
Reaching this code only requires crafting a transaction that (a) uses a non-owner `permission_id` (triggering the `permissionId != 0` branch that calls `checkPermissionOperations`) and (b) encodes an out-of-declared-range integer in the `Contract.type` field of the raw transaction bytes — both are fields fully controlled by an unprivileged transaction author/broadcaster before any signature or business validation examines the type's validity. No special privileges, witness/SR status, or chain state are required.

### Recommendation
Validate `contractType` against the declared, bounded range (e.g., `0 <= contractType < 256` and specifically against `ContractType.values()`) before using it to compute `operations.byteAt(...)`, and throw a `PermissionException` (as is already done for the `operations.size() != 32` case) instead of allowing an `IndexOutOfBoundsException` to escape. Apply the fix in both `WalletUtil.checkPermissionOperations` and `TransactionUtil.checkPermissionOperations` to keep the two implementations consistent.

### Proof of Concept
1. Construct a raw `Transaction` whose `raw_data.contract[0].permission_id` is a non-zero active permission id.
2. Manually encode the `Contract.type` field's wire value (field 1, varint) to an out-of-declared-range integer, e.g. `999999`, instead of one of the enumerated `ContractType` values (protobuf does not reject unknown enum wire values for this field).
3. Submit this transaction/serialized contract to any code path that ultimately calls `WalletUtil.checkPermissionOperations(permission, contract)` or `TransactionUtil.checkPermissionOperations(permission, contract)` with the permission whose `operations` bitmap is exactly 32 bytes.
4. `contractType / 8` evaluates to `124999`, far outside the valid `[0,31]` range for the 32-byte `ByteString`, causing `operations.byteAt(124999)` to throw `IndexOutOfBoundsException` instead of the intended `PermissionException`. [1](#0-0) [2](#0-1)

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
