## Title
Active permission operations bitmap is validated against the full `AvailableContractType` allowlist instead of the safer `ActiveDefaultOperations` set, letting an account owner (or a compromised low-weight Active key) grant a weak, low-threshold Active permission full authority over `AccountPermissionUpdateContract` — ([File: actuator/src/main/java/org/tron/core/actuator/AccountPermissionUpdateActuator.java])

### Summary
`AccountPermissionUpdateActuator.checkPermission()` validates the `operations` bitmap of an `Active` permission against `DynamicPropertiesStore.getAvailableContractType()` — the global list of *all currently enabled* contract types on chain — rather than against `getActiveDefaultOperations()`, which is the store's own designated "safe" set that deliberately excludes `AccountPermissionUpdateContract`. Because of this, the actuator accepts and persists an `Active` permission whose bitmap authorizes `AccountPermissionUpdateContract`, even though the codebase's own logic/tests treat that bit as one that must never be part of a delegated Active permission's authority.

### Finding Description
`checkPermission()` in [1](#0-0)  validates every bit set in an `Active` permission's `operations` field against `dynamicStore.getAvailableContractType()`. This store value represents the broad, "is this contract type enabled on chain at all" flag set — analogous to the coarse global `admin`/`builder` flags in the Budibase advisory — not a scope-restricted allowlist of operations that are safe to delegate to a lower-trust Active key.

The codebase separately maintains `ACTIVE_DEFAULT_OPERATIONS` via `DynamicPropertiesStore.getActiveDefaultOperations()/saveActiveDefaultOperations()` [2](#0-1) , and `addSystemContractAndSetPermission()` updates both bitmaps together when new contract types are enabled. The test suite explicitly documents that `AccountPermissionUpdateContract` must be excluded from `ActiveDefaultOperations` (`checkActiveDefaultOperationsCorrespondingToCode`, `checkActiveDefaultOperations`) while at the same time confirming that `AvailableContractType` *does* include `AccountPermissionUpdateContract` (`checkAvailableContractType`, `checkAvailableContractTypeCorrespondingToCode`) [3](#0-2) . In other words, the system's intended "scope" for what an Active permission may control excludes account-permission changes, but the actual authorization check enforced at `AccountPermissionUpdateActuator.checkPermission()` uses the wrong, broader set — never consulting `getActiveDefaultOperations()` at all. A `grep` over the actuator confirms `getActiveDefaultOperations` is never referenced there.

Consequently, any account owner constructing an `AccountPermissionUpdateContract` can set an `Active` permission (with an arbitrarily low threshold/weak key set, e.g. threshold 1 with a single low-weight key) whose `operations` bitmap includes the `AccountPermissionUpdateContract` bit, and `validate()` will accept it because the bit is present in `getAvailableContractType()`. Later, `TransactionCapsule.checkPermission()` — used at signature-verification time — only checks that the invoked permission is of type `Active` and that its operations bitmap authorizes the contract type being executed [4](#0-3) ; it has no additional restriction preventing an Active permission from being used to sign further `AccountPermissionUpdateContract` transactions. This means a signer holding only the weak Active key(s) can sign a subsequent `AccountPermissionUpdateContract` that rewrites the `Owner` permission entirely (installing attacker-controlled keys with full weight and dropping the legitimate owner's key), fully seizing/locking the account without ever needing to satisfy the (higher-security) Owner threshold.

### Impact Explanation
This breaks the core security guarantee of TRON's multi-permission model: that the high-threshold `Owner` permission is required to change account authority, while lower-trust `Active` permissions are meant to be restricted to non-sensitive, delegated operations. If an Active permission (handed to an exchange integration, automated bot, or lower-trust co-signer) is allowed to include the `AccountPermissionUpdateContract` bit, that weaker key set can unilaterally rewrite Owner/Witness/Active permissions, permanently locking out the legitimate owner and any other co-signers, or seizing full control of the account and its funds. This is a concrete unauthorized account-operation / permanent loss-of-control vulnerability reachable purely by an account owner's own signed transaction (self-inflicted misconfiguration is trivial to exploit against victims who are tricked into signing, or by any party that already controls just the Active key set).

### Likelihood Explanation
Reaching this requires only a single, unprivileged, signed `AccountPermissionUpdateContract` transaction — no special role, SR/witness/committee status, or node compromise is needed. Anyone broadcasting a transaction that has legitimate Owner-level signature authority (or anyone who already controls only the target account's Active key, e.g. because it was intentionally delegated for routine operations, or leaked via a lower-security channel) can craft the malicious `operations` bitmap. The validation gap is deterministic and always reachable through the public actuator's `validate()`/`execute()` path; no race condition or timing dependency is required.

### Recommendation
In `AccountPermissionUpdateActuator.checkPermission()`, validate the `Active` permission's `operations` bitmap against `dynamicStore.getActiveDefaultOperations()` (or an equivalent explicit denylist that always excludes `AccountPermissionUpdateContract`), not the broader `getAvailableContractType()`. At minimum, unconditionally reject any `Active` permission whose bitmap sets the bit corresponding to `ContractType.AccountPermissionUpdateContract`, matching the intent already encoded in the `ACTIVE_DEFAULT_OPERATIONS` bitmap and test suite.

### Proof of Concept
1. Owner account `A` currently has a standard Owner permission (threshold 2, keys `K1,K2`) and an Active permission delegated to a single low-trust key `K3` (threshold 1).
2. Owner (or holder of `K3`) submits an `AccountPermissionUpdateContract` where the new `Active` permission for `K3` sets `operations` with the bit for `ContractType.AccountPermissionUpdateContract` (`id=46`, per `Protocol.Transaction.Contract.ContractType`) turned on. `AccountPermissionUpdateActuator.validate()` accepts this because bit 46 is set in `dynamicStore.getAvailableContractType()` (confirmed by `checkAvailableContractType` test dump `7fff1fc0037ef90f...`), even though it is explicitly excluded from `getActiveDefaultOperations()` (`7fff1fc0033ef90f...` — note bit differing at byte 5).
3. `K3` alone (threshold 1, no need for `K1`/`K2`) can now sign a further `AccountPermissionUpdateContract` for account `A`, which `TransactionCapsule.checkPermission()` accepts since the Active permission's `operations` bit authorizes `AccountPermissionUpdateContract` [4](#0-3) .
4. That transaction replaces the Owner permission with a key fully controlled by the holder of `K3`, permanently locking out `K1`/`K2` and seizing full control of account `A`.

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

**File:** chainbase/src/main/java/org/tron/core/store/DynamicPropertiesStore.java (L1931-1960)
```java
  public void addSystemContractAndSetPermission(int id) {
    byte[] availableContractType = getAvailableContractType();
    availableContractType[id / 8] |= (1 << id % 8);
    saveAvailableContractType(availableContractType);

    byte[] activeDefaultOperations = getActiveDefaultOperations();
    activeDefaultOperations[id / 8] |= (1 << id % 8);
    saveActiveDefaultOperations(activeDefaultOperations);
  }

  public void updateDynamicStoreByConfig() {
    if (CommonParameter.getInstance()
        .getAllowTvmConstantinople() != 0) {
      saveAllowTvmConstantinople(CommonParameter.getInstance()
          .getAllowTvmConstantinople());
      addSystemContractAndSetPermission(48);
    }
  }

  public void saveActiveDefaultOperations(byte[] value) {
    this.put(ACTIVE_DEFAULT_OPERATIONS,
        new BytesCapsule(value));
  }

  public byte[] getActiveDefaultOperations() {
    return Optional.ofNullable(getUnchecked(ACTIVE_DEFAULT_OPERATIONS))
        .map(BytesCapsule::getData)
        .orElseThrow(
            () -> new IllegalArgumentException("not found ACTIVE_DEFAULT_OPERATIONS"));
  }
```

**File:** framework/src/test/java/org/tron/core/actuator/AccountPermissionUpdateActuatorTest.java (L919-1020)
```java
  @Test
  public void checkAvailableContractTypeCorrespondingToCode() {
    // note: The aim of this test case is to show how the current codes work.
    // The default value is
    // 7fff1fc0037e0000000000000000000000000000000000000000000000000000,
    // and it should call the addSystemContractAndSetPermission to add new contract
    // type
    // When you add a new contact, you can add it to contractType,
    // as '|| contractType = ContractType.XXX',
    // and you will get the value from the output,
    // then update the value to checkAvailableContractType
    // and checkActiveDefaultOperations
    String validContractType = "7fff1fc0037ef80f000000000000000000000000000000000000000000000000";

    byte[] availableContractType = new byte[32];
    for (ContractType contractType : ContractType.values()) {
      if (contractType == org.tron.protos.Protocol.Transaction.Contract.ContractType.UNRECOGNIZED
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

  }

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

  }

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

  }

  @Test
  public void checkActiveDefaultOperations() {
    String validContractType = "7fff1fc0033ef90f000000000000000000000000000000000000000000000000";

    byte[] availableContractType = new byte[32];
    for (ContractType contractType : ContractType.values()) {
      if (contractType == org.tron.protos.Protocol.Transaction.Contract.ContractType.UNRECOGNIZED
          || contractType == ContractType.AccountPermissionUpdateContract
          || contractType == ContractType.UpdateBrokerageContract) {
        continue;
      }
      int id = contractType.getNumber();
      System.out.println("id is " + id);
      availableContractType[id / 8] |= (1 << id % 8);
    }

    System.out.println(ByteArray.toHexString(availableContractType));

    Assert.assertEquals(validContractType, ByteArray.toHexString(availableContractType));

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
