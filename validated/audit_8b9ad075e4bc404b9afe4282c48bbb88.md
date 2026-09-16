### Title
Unbounded `ContractType` value in a signed transaction crashes permission/signature validation via `checkPermissionOperations` array index overflow - (File: `chainbase/src/main/java/org/tron/core/capsule/TransactionCapsule.java`)

### Summary
The GitLab CVE describes improper enforcement of the intended scope of a restricted credential (a Deploy Token) — the scope-check logic did not correctly bound what a scoped token could reach. The closest reachable analog in java-tron is the "Active" permission mechanism, which is exactly a scoped-credential system: an account can grant a sub-key an `Active` permission whose 256-bit `operations` bitmap restricts which `ContractType`s that key may authorize [1](#0-0) . The enforcement point, `checkPermissionOperations`, indexes into that 32-byte bitmap using the raw `contract.getTypeValue()` from the transaction without validating that the value is within the bitmap's bounds (0–255) [2](#0-1) [3](#0-2) .

### Finding Description
`Permission.operations` is a fixed 32-byte (256-bit) bitmap where 1 bit is defined per `ContractType`, and it's the mechanism that scopes a non-owner (`Active`) key's authority — directly analogous to a GitLab Deploy Token's restricted scope [1](#0-0) .

Both `WalletUtil.checkPermissionOperations` and the duplicate implementation in `TransactionUtil` compute:
```
int contractType = contract.getTypeValue();
boolean b = (operations.byteAt(contractType / 8) & (1 << (contractType % 8))) != 0;
```
`contract.getTypeValue()` returns the raw protobuf enum wire value of `Transaction.Contract.type`. Because protobuf enum fields are wire-compatible with arbitrary `int32` values, a transaction crafted with `Contract.Builder.setTypeValue(N)` for any `N` (including values well beyond the 256 legal `ContractType` values, e.g. `Integer.MAX_VALUE`) will not be rejected by protobuf deserialization — it simply becomes an "unrecognized" enum whose `getTypeValue()` still returns `N` [2](#0-1) .

This code path is exercised whenever a non-owner permission is used to sign/authorize a contract:
- `TransactionCapsule.checkPermission` (called from `validateSignature`, which is invoked from `validatePubSignature` during signature verification of every incoming transaction) calls `checkPermissionOperations(permission, contract)` for any `permissionId != 0` [4](#0-3) [5](#0-4) .
- `addSign` (used when incrementally co-signing a multisig transaction) also calls `checkPermission` before adding a signature [6](#0-5) .
- The same unguarded index computation exists in `Wallet.getTransactionApprovedList` and `TransactionUtil.getTransactionSignWeight`, both reachable from unauthenticated JSON-RPC/HTTP/gRPC query endpoints [7](#0-6) [8](#0-7) .

`ByteString.byteAt()` throws an unchecked `IndexOutOfBoundsException` when the index exceeds the 32-byte array, since it does not clamp or validate `contractType`. This exception type is **not** among the ones caught by `validatePubSignature`, which only handles `SignatureException | PermissionException | SignatureFormatException` [9](#0-8) . The `Wallet.getTransactionApprovedList` / `TransactionUtil.getTransactionSignWeight` HTTP/gRPC-facing entry points do catch generic `Exception`, so those specific query paths degrade gracefully to an `OTHER_ERROR` response rather than crashing [10](#0-9) ; however, the signature-verification path used when a transaction is broadcast/applied (`validatePubSignature`/`validateSignature`, called internally during transaction pool acceptance and block application in `Manager`) does not have this generic catch-all, so an uncaught `IndexOutOfBoundsException` can propagate out of the intended checked-exception boundary of transaction validation.

### Impact Explanation
If the uncaught `IndexOutOfBoundsException` propagates through the block-application/transaction-validation call chain in `Manager` without a surrounding catch, this can crash the block/transaction processing thread, causing the affected node to halt or repeatedly fail to process a block/transaction containing the malicious payload — a Denial of Service on transaction/block processing. Even where the exception is caught generically (the HTTP/gRPC query surfaces), it still demonstrates that the permission-scoping check (the java-tron analog of a Deploy Token's scope enforcement) is not properly bounds-validated against attacker-controlled input, which is the same root-cause class as the referenced CVE (broken/insufficiently validated scope enforcement for a restricted credential).

### Likelihood Explanation
Any unprivileged party who can construct a raw `Transaction` protobuf (not necessarily even successfully signed, since the bounds violation happens inside the operations-bitmap check that runs prior to/along with weight validation) can set an out-of-range `type` value on `Transaction.Contract` and target an account that has any `Active` permission configured with `permissionId != 0` (this is a very common configuration for TRON multisig accounts). No special privilege is required beyond broadcasting a transaction or invoking the sign-weight/approved-list query APIs.

### Recommendation
Bound-check `contractType` before indexing into the operations bitmap in both `WalletUtil.checkPermissionOperations` and `TransactionUtil.checkPermissionOperations`: reject (throw `PermissionException`) whenever `contractType < 0 || contractType >= 256` (or more precisely `>= operations.size() * 8`) instead of calling `operations.byteAt(...)` unconditionally. Additionally, ensure `TransactionCapsule.validatePubSignature`/`validateSignature` and any caller in `Manager` wrap the permission/signature validation call in a catch for generic `RuntimeException` (or explicitly for `IndexOutOfBoundsException`) so malformed input cannot escape as an unchecked exception during block application.

### Proof of Concept
1. Create/control an account `A` with a configured `Active` permission (`permissionId = 2`) with any valid 32-byte `operations` bitmap (the default multisig setup already includes this).
2. Construct a `Transaction` whose single `Contract` has `permission_id = 2` and use `Contract.Builder.setTypeValue(100000)` (or any value ≥ 256) instead of a valid `ContractType` enum constant — this passes protobuf encoding/decoding unchanged since `type` is wire-compatible with any int32.
3. Sign the transaction with a key present in permission id 2, or simply submit it unsigned to `GetTransactionSignWeight` / `GetTransactionApprovedList` / broadcast it via `wallet/broadcasttransaction`.
4. When `checkPermissionOperations` executes `operations.byteAt(100000 / 8)`, it throws `IndexOutOfBoundsException`, which propagates uncaught through `TransactionCapsule.checkPermission` → `validateSignature` → `validatePubSignature`, since only `SignatureException`, `PermissionException`, and `SignatureFormatException` are handled there [9](#0-8) .

Note: I was not able to fully trace every downstream catch-block in `framework/src/main/java/org/tron/core/db/Manager.java` (its `validateSignature`/`validatePubSignature` call sites) within the available search results to conclusively confirm whether a node-crashing halt occurs versus a per-transaction rejection; this should be verified directly against `Manager.java` before treating the "node halt" impact as certain. The bounds-check gap itself in `checkPermissionOperations`, however, is confirmed directly from the source shown above.

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

**File:** actuator/src/main/java/org/tron/core/utils/TransactionUtil.java (L236-244)
```java
        if (permissionId != 0) {
          if (permission.getType() != PermissionType.Active) {
            throw new PermissionException("Permission type is wrong!");
          }
          //check operations
          if (!checkPermissionOperations(permission, contract)) {
            throw new PermissionException("Permission denied!");
          }
        }
```

**File:** chainbase/src/main/java/org/tron/core/capsule/TransactionCapsule.java (L468-496)
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
    if (weight >= permission.getThreshold()) {
      return true;
    }
    return false;
  }
```

**File:** chainbase/src/main/java/org/tron/core/capsule/TransactionCapsule.java (L597-633)
```java
  public void addSign(byte[] privateKey, AccountStore accountStore)
      throws PermissionException, SignatureException, SignatureFormatException {
    Transaction.Contract contract = this.transaction.getRawData().getContract(0);
    int permissionId = contract.getPermissionId();
    byte[] owner = getOwnerAddress();
    AccountCapsule account = accountStore.get(owner);
    if (account == null) {
      throw new PermissionException("Account is not exist!");
    }
    Permission permission = account.getPermissionById(permissionId);
    if (permission == null) {
      throw new PermissionException("permission isn't exit");
    }
    checkPermission(permissionId, permission, contract);
    List<ByteString> approveList = new ArrayList<>();
    SignInterface cryptoEngine = SignUtils
        .fromPrivate(privateKey, CommonParameter.getInstance().isECKeyCryptoEngine());
    byte[] address = cryptoEngine.getAddress();
    if (this.transaction.getSignatureCount() > 0) {
      checkWeight(permission, this.transaction.getSignatureList(),
          this.getTransactionId().getBytes(),
          approveList);
      if (approveList.contains(ByteString.copyFrom(address))) {
        throw new PermissionException(encode58Check(address) + " had signed!");
      }
    }

    long weight = getWeight(permission, address);
    if (weight == 0) {
      throw new PermissionException(
          ByteArray.toHexString(privateKey) + "'s address is " + encode58Check(address)
              + " but it is not contained of permission.");
    }
    ByteString sig = ByteString.copyFrom(cryptoEngine.Base64toBytes(cryptoEngine
        .signHash(getTransactionId().getBytes())));
    this.transaction = this.transaction.toBuilder().addSignature(sig).build();
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

**File:** framework/src/main/java/org/tron/core/Wallet.java (L670-678)
```java
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

**File:** framework/src/main/java/org/tron/core/Wallet.java (L694-697)
```java
      } catch (Exception ex) {
        resultBuilder.setCode(TransactionApprovedList.Result.response_code.OTHER_ERROR);
        resultBuilder.setMessage(ex.getClass() + " : " + ex.getMessage());
      }
```
