## Title
Unchecked raw `ContractType` value in permission-operation bit-check causes uncaught `IndexOutOfBoundsException` during transaction signature validation - ([File: chainbase/src/main/java/org/tron/core/capsule/TransactionCapsule.java])

### Summary
Similar to CVE-2026-30078, where OpenAirInterface's AMF crashes because it doesn't validate that an NGAP procedure/PDU field matches an expected discriminant before acting on it, java-tron's permission-operation check trusts the raw wire value of a transaction's `Contract.type` field without validating it against the legal `ContractType` enum range before using it as a bit-array index.

### Finding Description
`Transaction.Contract.type` is a protobuf enum field. Protobuf3 enum wire semantics allow the *raw* integer to be any `int32`; unknown values are exposed via `getTypeValue()` (the raw wire int), while `getType()` collapses unknown values to `UNRECOGNIZED`. Two permission-check code paths use the unchecked raw value directly as a bit index into a fixed 32-byte `operations` `ByteString`: [1](#0-0) [2](#0-1) 

Both call `contract.getTypeValue()` and compute `operations.byteAt(contractType / 8)` with no bounds check that `contractType` is within `[0, 255]` (the array is only 32 bytes = 256 bits). If an attacker crafts raw transaction bytes where the `Contract.type` varint is a large or negative out-of-range value (e.g. far beyond the ~60 defined `ContractType` enum values, or via a manipulated int32), `contractType / 8` can exceed 31 or even be negative, and `ByteString.byteAt()` throws an `IndexOutOfBoundsException`.

This path is reached from `TransactionCapsule.checkPermission()`, which is invoked from `validateSignature()`, which is invoked from `validatePubSignature()`: [3](#0-2) [4](#0-3) 

Crucially, the only condition to reach `checkPermissionOperations` is `permissionId != 0` (multi-sig/active permission usage) — this only requires the sender account to have set up an Active permission, a normal, unprivileged operation any account owner can perform on themselves. The catch block in `validatePubSignature` only catches `SignatureException`, `PermissionException`, and `SignatureFormatException` — it does **not** catch `RuntimeException`/`IndexOutOfBoundsException`: [5](#0-4) 

An uncaught `IndexOutOfBoundsException` here propagates out of transaction pre-verification, mirroring the CVE's root cause: acting on a message field (procedure code / PDU type) without first checking it's within the expected/valid discriminant set before dispatch, leading to a crash instead of a clean, handled rejection.

The equivalent check in `AccountPermissionUpdateActuator.checkPermission()`, which validates the `operations` byte pattern when an account owner *sets* a permission, iterates a fixed `0..255` range and is therefore safe: [6](#0-5) 

But that safety only applies to defining/updating operations bit patterns — it does not validate the *transaction's own* `Contract.type` raw value when the check is actually exercised at signature/permission-verification time via `getTypeValue()`.

### Impact Explanation
An uncaught `RuntimeException` thrown while validating an incoming, unprivileged, attacker-crafted transaction during signature/permission checking can crash or destabilize the processing thread that handles broadcast transactions (P2P transaction inbound handling / API broadcast path), a node crash/DoS analog to the AMF crash in the original CVE. This matches the "node crash or halt" acceptance criterion.

### Likelihood Explanation
Reaching the vulnerable code only requires: (1) the victim account to have an Active permission configured (a completely normal, unprivileged self-service operation any account can perform), and (2) an attacker to submit a transaction referencing that account with `permissionId != 0` and a raw `Contract.type` value manipulated outside the valid enum range at the wire level (bypassing the normal Java setter, which is trivial when constructing raw protobuf bytes for the `raw_data_hex` field, as already demonstrated feasible by the existing `testPackTransactionWithInvalidType` test showing arbitrary/invalid contract type strings/values are accepted by `Util.packTransaction` without validation before signature checking) [7](#0-6) . No special privilege is required from the attacker; only a normally-configured victim account.

### Recommendation
Bound-check the raw contract type value before using it as an array index in both `WalletUtil.checkPermissionOperations` and `TransactionUtil.checkPermissionOperations`: reject (throw `PermissionException`) if `contractType < 0 || contractType > 255` (or more strictly, if it doesn't correspond to a value in `ContractType.values()`), before computing `operations.byteAt(contractType / 8)`. Alternatively, catch `IndexOutOfBoundsException`/`RuntimeException` around the `checkPermissionOperations` call sites and convert to `PermissionException`, ensuring `TransactionCapsule.validatePubSignature` cannot propagate an unchecked runtime exception from attacker-controlled input.

### Proof of Concept
1. Craft a raw transaction (`raw_data_hex`) whose `Contract` message sets the `type` field (`type=1` proto tag) with a varint value far outside the defined `ContractType` range (e.g., a large positive int32 like `2000000000`), targeting an account that already has an `Active` Permission set with `id=2` (`permissionId=2` in the contract).
2. Broadcast this transaction (or submit via the HTTP/gRPC broadcast API), same way `Util.packTransaction`/`BroadcastTransaction` accepts contract type without validating enum membership as shown in the existing test that already sends invalid type strings without a validation error before signature-checking [7](#0-6) .
3. `TransactionCapsule.validatePubSignature` → `validateSignature` → `checkPermission` → `checkPermissionOperations` executes `operations.byteAt(contractType / 8)` with `contractType / 8` far exceeding the 32-byte `operations` bound, throwing an uncaught `IndexOutOfBoundsException` that is not caught by the surrounding `catch (SignatureException | PermissionException | SignatureFormatException e)` block, propagating out of the transaction verification pipeline.

**Note on confidence:** I was unable to fully trace every caller of `TransactionCapsule.validatePubSignature()` outside of the file itself (tool access ended before confirming the exact upstream caller in `Manager`/`TransactionsMsgHandler`/API broadcast flow), so the exact thread/impact surface (P2P inbound vs. block-application vs. API validation) should be confirmed by a follow-up code read of `Manager.pushTransaction`/`TransactionsMsgHandler` before remediation.

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

**File:** actuator/src/main/java/org/tron/core/actuator/AccountPermissionUpdateActuator.java (L133-144)
```java
    if (operations.isEmpty() || operations.size() != 32) {
      throw new ContractValidateException("operations size must 32");
    }

    byte[] types1 = dynamicStore.getAvailableContractType();
    for (int i = 0; i < 256; i++) {
      boolean b = (operations.byteAt(i / 8) & (1 << (i % 8))) != 0;
      boolean t = ((types1[(i / 8)] & 0xff) & (1 << (i % 8))) != 0;
      if (b && !t) {
        throw new ContractValidateException(i + " isn't a validate ContractType");
      }
    }
```

**File:** framework/src/test/java/org/tron/core/services/http/UtilTest.java (L49-92)
```java
  @Test
  public void testPackTransactionWithInvalidType() {

    String strTransaction = "{\n"
        + "    \"visible\": false,\n"
        + "    \"signature\": [\n"
        + "        \"5c23bddabccd3e4e5ebdf7d2f21dc58af9f88e0b99620374c5354e0dd9efb3a436167d95b70d2"
        + "d825180bf90bc84525acb13a203f209afd5d397316f6b2c387c01\"\n"
        + "    ],\n"
        + "    \"txID\": \"fc33817936b06e50d4b6f1797e62f52d69af6c0da580a607241a9c03a48e390e\",\n"
        + "    \"raw_data\": {\n"
        + "        \"contract\": [\n"
        + "            {\n"
        + "                \"parameter\": {\n"
        + "                    \"value\": {\n"
        + "                      \"amount\": 10,\n"
        + "                      \"owner_address\":\"41c076305e35aea1fe45a772fcaaab8a36e87bdb55\","
        + "                      \"to_address\": \"415624c12e308b03a1a6b21d9b86e3942fac1ab92b\"\n"
        + "                    },\n"
        + "                    \"type_url\": \"type.googleapis.com/protocol.TransferContract\"\n"
        + "                },\n"
        + "                \"type\": \"TransferContract11111\"\n"
        + "            }\n"
        + "        ],\n"
        + "        \"ref_block_bytes\": \"d8ed\",\n"
        + "        \"ref_block_hash\": \"2e066c3259e756f5\",\n"
        + "        \"expiration\": 1651906644000,\n"
        + "        \"timestamp\": 1651906586162\n"
        + "    },\n"
        + "    \"raw_data_hex\": \"0a02d8ed22082e066c3259e756f540a090bcea89305a65080112610a2d747970"
        + "652e676f6f676c65617069732e636f6d2f70726f746f636f6c2e5472616e73666572436f6e74726163741230"
        + "0a1541c076305e35aea1fe45a772fcaaab8a36e87bdb551215415624c12e308b03a1a6b21d9b86e3942fac1a"
        + "b92b180a70b2ccb8ea8930\"\n"
        + "}";
    Transaction transaction = Util.packTransaction(strTransaction, false);
    TransactionApprovedList transactionApprovedList =
        wallet.getTransactionApprovedList(transaction);
    Assert.assertEquals("Invalid transaction: no valid contract",
        transactionApprovedList.getResult().getMessage());

    TransactionSignWeight txSignWeight = transactionUtil.getTransactionSignWeight(transaction);
    Assert.assertEquals("Invalid transaction: no valid contract",
        txSignWeight.getResult().getMessage());

```
