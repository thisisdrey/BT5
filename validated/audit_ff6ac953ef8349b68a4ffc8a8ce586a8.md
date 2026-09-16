### Title
Integer overflow/out-of-bounds bypass and crash in multisig permission-operations check via unvalidated `ContractType` raw value - (File: `chainbase/src/main/java/org/tron/common/utils/WalletUtil.java`)

### Summary
The multisig "Active" permission enforcement in java-tron checks whether a signer's `Permission.operations` bitmap authorizes a given `Contract`'s type by indexing a fixed 32-byte bitmap with the *raw* protobuf enum value of the contract, rather than a bounds-checked/validated `ContractType`. Because `ContractType` is an open proto3 enum, an attacker fully controls the raw `type` field of a `Transaction.Contract`, including out-of-range and negative values that do not correspond to any real, permission-checked contract kind. This can either crash the transaction/signature validation path (denial of service) or produce incorrect authorization results due to sign-extension/rounding behavior of the bit math, analogous to the Kyverno bug where the policy comparison logic failed to correctly evaluate a "special-case" value (`latest`) and thereby let the intended security check be skipped.

### Finding Description
The check lives in `WalletUtil.checkPermissionOperations`: [1](#0-0) 

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
```

`contract.getTypeValue()` returns the *raw* wire-level int32 of the `Contract.type` field, not the validated `ContractType` enum. Because `ContractType` is declared as a standard (open) proto3 enum, any int32 value in that field parses successfully; unrecognized values are preserved verbatim and `getTypeValue()` will return them even though `getType()` would report `UNRECOGNIZED`. This means a transaction author fully controls `contractType`, including negative values and values far outside the 0–255 range that the fixed 32-byte (256-bit) `operations` bitmap is designed for.

This method is invoked directly on the primary signature-verification hot path used to authorize every multisig ("Active" permission, `permissionId != 0`) transaction: [2](#0-1) 

and from the JSON/HTTP-reachable `getTransactionSignWeight` API used to evaluate arbitrary attacker-supplied transactions for permission sufficiency: [3](#0-2) 

Both call sites pass an attacker-controlled `Contract` (from `trx.getRawData().getContract(0)`) straight into `checkPermissionOperations` before any actuator-level rejection of unrecognized contract types occurs (contract-type-specific actuator dispatch happens later in the pipeline, not before this permission gate).

With `contractType` attacker-controlled and unbounded:
- If `contractType` is negative (e.g., `-8`), Java integer division truncates toward zero, so `contractType / 8` can be a small negative index, and `ByteString.byteAt(negativeIndex)` throws an `IndexOutOfBoundsException`. This exception type is **not** in the catch list of the calling `validatePubSignature`/`getTransactionSignWeight` code paths (`SignatureException | PermissionException | SignatureFormatException`), so it propagates as an uncaught runtime exception.
- If `contractType` is large (e.g., `> 255`), `contractType / 8` exceeds index 31, again throwing an out-of-bounds exception on a 32-byte `ByteString`.
- Java's `<<` operator masks the shift amount to the low 5 bits (`contractType % 8` is always small so this specific expression is less exploitable for logic bypass, but the index computation above is the primary hazard).

### Impact Explanation
Because `validatePubSignature` (via `TransactionCapsule.validateSignature` → `checkPermission` → `checkPermissionOperations`) sits on the transaction-acceptance path used both for mempool/broadcast validation and for the `getTransactionSignWeight` Wallet API, a single crafted transaction from any unprivileged sender that uses `permission_id != 0` (an "Active" sub-key) together with a raw contract `type` value outside the valid enumerated range can throw an unhandled `IndexOutOfBoundsException`. Depending on how far up the call stack this propagates without being caught, this can disrupt transaction processing / block application (denial of service, "node crash or halt") or, if a request is served through an API without a defensive top-level catch, can crash that request-handling thread and make an exposed API unusable to legitimate callers.

### Likelihood Explanation
The `type` field of `Transaction.Contract` is fully attacker-controlled raw protobuf data; no signature or prior structural check constrains it to a value in the officially enumerated `ContractType` range before it reaches `checkPermissionOperations`. Any account that has configured a multisig "Active" permission (a normal, unprivileged account feature) can trigger this by submitting or querying (via `getTransactionSignWeight`) a transaction whose `Contract.type` raw value is negative or > 255. This requires no special privileges — only a signed transaction using `permission_id` and a crafted contract type — making it directly reachable by any transaction broadcaster or anonymous API client using `getTransactionSignWeight`.

### Recommendation
Validate `contract.getType()` against the recognized `ContractType` enum (rejecting `UNRECOGNIZED`) before using it as a bitmap index, and/or bounds-check `contractType` (e.g., `0 <= contractType < 256`) inside `checkPermissionOperations` prior to calling `operations.byteAt(...)`, throwing a `PermissionException` for out-of-range values instead of allowing the raw value to reach the bit-index arithmetic.

### Proof of Concept
1. Craft a `Transaction` whose `raw_data.contract[0].type` raw wire value is set to a value outside `[0,255]` (e.g., `-8` or `300`) — proto3 will happily encode/parse this even though it is not one of the declared `ContractType` enum constants.
2. Set `permission_id = 2` (an "Active" permission) on the contract, referencing an account that has configured a valid Active permission with an `operations` bitmap of size 32.
3. Submit this transaction to `Wallet.getTransactionSignWeight` (or broadcast it so `validatePubSignature` runs) with at least one valid signature under that active key.
4. Execution reaches `WalletUtil.checkPermissionOperations` at [4](#0-3) 
where `contractType / 8` is out of the valid `[0,31]` range for the 32-byte `operations` `ByteString`, causing `ByteString.byteAt` to throw an unhandled `IndexOutOfBoundsException` that is not caught by the surrounding `SignatureException | PermissionException | SignatureFormatException` handlers in [5](#0-4) .

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

**File:** actuator/src/main/java/org/tron/core/utils/TransactionUtil.java (L231-243)
```java
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
          if (!checkPermissionOperations(permission, contract)) {
            throw new PermissionException("Permission denied!");
          }
```

**File:** actuator/src/main/java/org/tron/core/utils/TransactionUtil.java (L259-271)
```java
      } catch (SignatureFormatException signEx) {
        resultBuilder.setCode(Result.response_code.SIGNATURE_FORMAT_ERROR);
        resultBuilder.setMessage(signEx.getMessage());
      } catch (SignatureException signEx) {
        resultBuilder.setCode(Result.response_code.COMPUTE_ADDRESS_ERROR);
        resultBuilder.setMessage(signEx.getMessage());
      } catch (PermissionException permEx) {
        resultBuilder.setCode(Result.response_code.PERMISSION_ERROR);
        resultBuilder.setMessage(permEx.getMessage());
      } catch (Exception ex) {
        resultBuilder.setCode(Result.response_code.OTHER_ERROR);
        resultBuilder.setMessage(ex.getClass() + " : " + ex.getMessage());
      }
```
