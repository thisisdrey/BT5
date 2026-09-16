Based on the investigation, the closest reachable analog in java-tron to the "lower-privilege role can undo/override higher-privilege role" bug class is in the account multi-permission (`Owner`/`Witness`/`Active`) system, specifically `AccountPermissionUpdateActuator` and the transaction-level permission checks in `TransactionCapsule`.

### Title
Active-tier permission can be granted rights to overwrite Owner permission with no on-chain restriction, enabling permanent account takeover by a lower-trust key - (File: actuator/src/main/java/org/tron/core/actuator/AccountPermissionUpdateActuator.java)

### Summary
TRON's account permission model defines a trust hierarchy: `Owner` (highest, full control including re-keying the account), `Witness` (SR-specific), and `Active` (restricted, only allowed to invoke the contract types explicitly whitelisted in its `operations` bitmap). The convention/SDK-level expectation is that `Active` keys are lower-privileged delegated keys that should never be able to seize full account control, i.e. they must not be allowed to execute `AccountPermissionUpdateContract` itself. However, the on-chain actuator validation for building an `Active` permission's `operations` bitmap does not exclude `AccountPermissionUpdateContract` (or other sensitive types) - it only checks the bits against the global `getAvailableContractType()` whitelist, which includes this contract type. Once an `Active` permission's bitmap includes this bit (whether by owner misconfiguration, a compromised wallet default, or a crafted "convenience" multi-sig setup), that lower-trust `Active` key can independently execute further `AccountPermissionUpdateContract` transactions and completely rewrite the `Owner` permission, permanently locking out the legitimate high-trust key(s) - the reverse of the intended hierarchy, directly analogous to `registryKeeper` (low trust) undoing an action reserved for `panicButton`/`Governance` (high trust).

### Finding Description
`AccountPermissionUpdateActuator.checkPermission()` validates an `Active` permission's `operations` bitmap only against the chain-wide `dynamicStore.getAvailableContractType()` bitmap: [1](#0-0) 

This global whitelist includes `AccountPermissionUpdateContract` by default, as shown by the test asserting the default `availableContractType` bitmap covers essentially every contract type except `UpdateBrokerageContract`: [2](#0-1) 

By contrast, the SDK/actuator-side *default* `Active` permission created for a fresh account is intentionally narrower and excludes `AccountPermissionUpdateContract`, `ClearABIContract`, and `UpdateBrokerageContract` - showing this exclusion is understood to be a security-relevant restriction, not an oversight: [3](#0-2) 

Yet nothing in `checkPermission()` (actuator/src/main/java/org/tron/core/actuator/AccountPermissionUpdateActuator.java:71-146) enforces this exclusion for *custom* `Active` permissions submitted via `AccountPermissionUpdateContract`. Once such a permission is stored, transaction execution/signature validation treats it like any other `Active` permission: `TransactionCapsule.validateSignature()` and `TransactionCapsule.checkPermission()` only require that the permission type is `Active` and that the target contract type bit is set in its `operations`, with no special-case rejecting `AccountPermissionUpdateContract` from being invoked via a non-zero `permissionId`: [4](#0-3) [5](#0-4) 

The same unrestricted check is duplicated in `TransactionUtil.checkPermissionOperations`/`getTransactionSignWeight` and `WalletUtil.checkPermissionOperations`, used by `Wallet.broadcastTransaction`-adjacent RPC paths, meaning any signed transaction reaching these code paths with an `Active` permission holding the relevant bit is honored the same way regardless of the sensitivity of the underlying contract type: [6](#0-5) [7](#0-6) 

### Impact Explanation
If an `Active` permission ever ends up including the `AccountPermissionUpdateContract` bit (e.g. via a wallet/tool that doesn't mirror the "safe defaults", a socially-engineered multisig setup, or an Owner-signed permission update that an account holder doesn't fully audit), the holder of that lower-trust `Active` key gains a durable, undetectable path to fully overwrite the account's `Owner` permission at will - with no further Owner-tier confirmation required. This is a permanent, unauthorized account takeover: the attacker can lock the legitimate high-trust key(s) out entirely and redirect all authority (including subsequent balance transfers, further permission changes, and resource operations) to itself. This is a concrete unauthorized account operation / permanent loss of control over funds and account governance, matching the "cannot be undone by lower-trust role" bug class from the report.

### Likelihood Explanation
Exploitability requires that an `Active` permission's `operations` bitmap includes the `AccountPermissionUpdateContract` bit. This is not the default configuration produced by the reference wallet defaults (which explicitly excludes it), but the protocol/actuator layer provides no enforcement preventing it, so any tooling, custom wallet, or multisig setup script that composes the `operations` bitmap more permissively (e.g., "allow all contract types" convenience helpers, which are common in integrations) will silently create this escalation path. Because the check is purely bit-based, a single overly-broad `Active` permission update transaction (which requires only an ordinary signed transaction, not any special node/committee/witness privilege) is sufficient to introduce the flaw, and a single subsequent transaction from the `Active` key exploits it.

### Recommendation
Add an explicit, protocol-level restriction in `AccountPermissionUpdateActuator.checkPermission()` (and mirror it in `TransactionCapsule`/`TransactionUtil`/`WalletUtil` permission checks) that unconditionally rejects any `Active` permission `operations` bitmap containing the `AccountPermissionUpdateContract` bit (and ideally other account-authority-changing contract types), regardless of the chain's `getAvailableContractType()` whitelist. This closes the gap between the intended trust hierarchy (`Owner` > `Active`) and what the chain currently allows to be encoded and executed.

### Proof of Concept
1. Owner account A creates/updates its permissions via `AccountPermissionUpdateContract`, defining an `Active` permission P with key K, where P's 32-byte `operations` bitmap has the bit for `AccountPermissionUpdateContract` (contract type value, per `ContractType.AccountPermissionUpdateContract.getNumber()`) set to 1 (this passes `checkPermission()` unmodified since it only checks against `getAvailableContractType()`, which includes this type).
2. Using key K (holder of only the `Active` permission P, not the `Owner` permission), submit a new `AccountPermissionUpdateContract` transaction for account A, signed with `permission_id` referencing P, setting a brand-new `Owner` permission controlled entirely by attacker-held keys.
3. `TransactionCapsule.validateSignature`/`checkPermission` accepts this because P is `Active`-typed and its `operations` bitmap has the `AccountPermissionUpdateContract` bit set; `AccountPermissionUpdateActuator.execute()` then calls `AccountCapsule.updatePermissions()`, unconditionally replacing the `Owner` permission with the attacker's.
4. Account A's original Owner keys are now permanently locked out; all further authority over the account belongs to the attacker.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/AccountPermissionUpdateActuator.java (L132-145)
```java
    //check operations
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
    return true;
```

**File:** framework/src/test/java/org/tron/core/actuator/AccountPermissionUpdateActuatorTest.java (L951-976)
```java
  @Test
  public void checkActiveDefaultOperationsCorrespondingToCode() {
    // note: The aim of this test case is to show how the current codes work.
    // The default value is
    // 7fff1fc0033e0000000000000000000000000000000000000000000000000000,
    // and it should call the addSystemContractAndSetPermission to add new contract
    // type
    String validContractType = "7fff1fc0033ef80f000000000000000000000000000000000000000000000000";

    byte[] availableContractType = new byte[32];
    for (ContractType contractType : ContractType.values()) {
      if (contractType == org.tron.protos.Protocol.Transaction.Contract.ContractType.UNRECOGNIZED
          || contractType == ContractType.AccountPermissionUpdateContract
          || contractType == ContractType.ClearABIContract
          || contractType == ContractType.UpdateBrokerageContract) {
        continue;
      }
      int id = contractType.getNumber();
      System.out.println("id is " + id);
      availableContractType[id / 8] |= (1 << id % 8);
    }

    System.out.println(ByteArray.toHexString(availableContractType));

    Assert.assertEquals(validContractType, ByteArray.toHexString(availableContractType));

```

**File:** framework/src/test/java/org/tron/core/actuator/AccountPermissionUpdateActuatorTest.java (L979-997)
```java
  @Test
  public void checkAvailableContractType() {
    String validContractType = "7fff1fc0037ef90f000000000000000000000000000000000000000000000000";

    byte[] availableContractType = new byte[32];
    for (ContractType contractType : ContractType.values()) {
      if (contractType == org.tron.protos.Protocol.Transaction.Contract.ContractType.UNRECOGNIZED
          || contractType == ContractType.UpdateBrokerageContract) {
        continue;
      }
      int id = contractType.getNumber();
      System.out.println("id is " + id);
      availableContractType[id / 8] |= (1 << id % 8);
    }

    System.out.println(ByteArray.toHexString(availableContractType));

    Assert.assertEquals(validContractType, ByteArray.toHexString(availableContractType));

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
