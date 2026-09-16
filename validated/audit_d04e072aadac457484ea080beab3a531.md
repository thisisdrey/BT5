### Title
Privilege escalation via `AccountPermissionUpdateContract` — an Active-permission key with the `AccountPermissionUpdateContract` operation bit can rewrite the account's Owner permission without Owner-level authorization - (File: `actuator/src/main/java/org/tron/core/actuator/AccountPermissionUpdateActuator.java`)

### Summary
`AccountPermissionUpdateActuator.validate()` only checks the *structural* validity of the new Owner/Witness/Active permissions submitted in an `AccountPermissionUpdateContract` (key counts, weights, threshold, operations bitmap, etc.). It never checks that the *authority invoking the update* already holds Owner-level trust. Authorization for this actuator, like every other contract type, is delegated to the generic Active-permission operations-bitmap mechanism (`checkPermissionOperations`/`checkWeight`) used uniformly for all `ContractType`s. As a result, if an account owner ever grants the `AccountPermissionUpdateContract` bit to a lower-threshold Active permission (a legitimate-looking delegation, e.g. for automated key rotation), the holder of that Active key can use it to overwrite the account's Owner permission itself, seizing full administrative control of the account without ever satisfying the Owner permission's threshold.

### Finding Description
`AccountPermissionUpdateActuator.checkPermission()` validates the shape of the submitted `Permission` objects but performs no authority check relative to the signer: [1](#0-0) 

The only gate for *who* may submit this contract is the generic multisig framework, which resolves the permission to use purely from the `Permission_id` field the sender embeds in the transaction, and then checks only whether that permission's operations bitmap has the bit set for the contract type being executed: [2](#0-1) [3](#0-2) 

The same pattern (permission chosen by `permissionId`, then only an operations-bit check, then a weight check against *that* permission's own threshold — not the Owner permission's threshold) is repeated at broadcast-time signature validation: [4](#0-3) 

Nowhere in this chain is there a requirement that modifying the **Owner** permission (or Witness permission) must itself be authorized by the current Owner permission (`permissionId == 0`). Any Active permission that has the `AccountPermissionUpdateContract` operation bit enabled is sufficient, and the weight/threshold enforced is that Active permission's own (potentially very low) threshold, not the Owner permission's threshold.

Tellingly, the codebase's own default-active-permission construction explicitly excludes this bit from the default bitmap, showing the developers were aware this operation is uniquely dangerous to hand out on an Active key: [5](#0-4) 

Yet `checkPermission()`'s bitmap validation only checks against `getAvailableContractType()`, which does allow this bit to be turned on for an Active permission: [6](#0-5) [7](#0-6) 

This mirrors the Foreman CVE-2026-5136 pattern exactly: a role-assignment/permission-management capability (here, an Active permission carrying the `AccountPermissionUpdateContract` bit) is not validated against the caller's actual authority level, allowing that caller to attach/assign a higher-privilege role (rewrite the account's Owner permission naming themselves as sole key) and thereby escalate to full administrative control of the account.

### Impact Explanation
An attacker who controls only a scoped Active key (e.g. a delegated automation/service key with threshold 1, meant only for limited operations) — provided that key's operations bitmap also carries the `AccountPermissionUpdateContract` bit — can broadcast a single `AccountPermissionUpdateContract` transaction that:
- Sets the account's `owner_permission` to a permission whose sole key is the attacker's address with weight ≥ threshold.
- Optionally strips/rewrites the Witness permission and all other Active permissions.

This completely and permanently locks out the legitimate account owner and any co-signers, and hands full control of the account (including subsequent unrestricted transfers, freezing/voting, further permission rewrites, and smart-contract calls) to the attacker. This is unauthorized account takeover — a High severity impact matching the CVSS vector of the Foreman analog (PR:L, C:H/I:H/A:H for the affected account).

### Likelihood Explanation
Exploitation requires the attacker to already hold an Active key whose operations bitmap includes the `AccountPermissionUpdateContract` bit. This is not the default configuration, but it is a normal, sanctioned delegation pattern in TRON multisig (owners commonly delegate permission-management to automation or secondary signers for convenience, e.g. periodic key rotation), and nothing in the actuator or the wallet-side documentation/UX (`AccountPermissionUpdateServlet`, `Wallet`) warns that granting this single bit is equivalent to granting full Owner control. Given how easy it is for an account owner to enable this bit (a single-bit flip in the 32-byte `operations` field) without realizing its consequences, and that the resulting escalation requires only meeting the low Active threshold rather than the Owner threshold, likelihood of accidental or social-engineered exposure is realistic.

### Recommendation
Enforce that any `AccountPermissionUpdateContract` transaction which modifies the Owner permission (and/or Witness permission) must be authorized by the account's *current* Owner permission (`permissionId == 0`), regardless of whether an Active permission's operations bitmap happens to include this bit. Concretely:
- In `AccountPermissionUpdateActuator.validate()`, require that the contract's `Permission_id` used to authorize the transaction is `0` (Owner), or otherwise verify the current Owner permission's threshold is independently satisfied before allowing changes to `owner_permission`/`witness_permission`.
- Alternatively, remove `AccountPermissionUpdateContract` from the set of operations that can ever be enabled on an Active permission's bitmap in `checkPermission()`'s validation against `getAvailableContractType()`, consistent with how it is already excluded from the default active bitmap.

### Proof of Concept
1. Account owner `O` creates an Active permission `P2` (id=2, threshold=1) with a single delegated key `K` (weight=1), and sets its `operations` bitmap with bit 46 (`AccountPermissionUpdateContract`) enabled alongside other intended bits (e.g. Transfer), via a normal `AccountPermissionUpdateContract` signed with the Owner key.
2. Attacker who controls only private key `K` builds a new `AccountPermissionUpdateContract` for account `O` with:
   - `owner`: threshold=1, single key = attacker's own address, weight=1.
   - `actives`: unchanged or attacker-controlled.
3. Attacker signs this transaction using only `K`. `TransactionUtil.checkPermissionOperations`/`TransactionCapsule.checkPermission` see permission `P2` has bit 46 set, so the contract type check passes; `checkWeight` only requires meeting `P2`'s threshold (1), which `K`'s signature satisfies.
4. Broadcast succeeds; `AccountPermissionUpdateActuator.execute()` overwrites `owner_permission` on account `O` with the attacker-controlled permission, per `checkPermission(owner)` at [8](#0-7) , granting the attacker sole administrative control of account `O`.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/AccountPermissionUpdateActuator.java (L124-145)
```java
    ByteString operations = permission.getOperations();
    if (permission.getType() != PermissionType.Active) {
      if (!operations.isEmpty()) {
        throw new ContractValidateException(
            permission.getType() + " permission needn't operations");
      }
      return true;
    }
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

**File:** actuator/src/main/java/org/tron/core/actuator/AccountPermissionUpdateActuator.java (L208-228)
```java
    Permission owner = accountPermissionUpdateContract.getOwner();
    Permission witness = accountPermissionUpdateContract.getWitness();
    List<Permission> actives = accountPermissionUpdateContract.getActivesList();

    if (owner.getType() != PermissionType.Owner) {
      throw new ContractValidateException("owner permission type is error");
    }
    checkPermission(owner);
    if (accountCapsule.getIsWitness()) {
      if (witness.getType() != PermissionType.Witness) {
        throw new ContractValidateException("witness permission type is error");
      }
      checkPermission(witness);
    }
    for (Permission permission : actives) {
      if (permission.getType() != PermissionType.Active) {
        throw new ContractValidateException("active permission type is error");
      }
      checkPermission(permission);
    }
    return true;
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

**File:** actuator/src/main/java/org/tron/core/utils/TransactionUtil.java (L231-253)
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
        }
        tswBuilder.setPermission(permission);
        if (trx.getSignatureCount() > 0) {
          List<ByteString> approveList = new ArrayList<>();
          long currentWeight = TransactionCapsule.checkWeight(permission, trx.getSignatureList(),
              Sha256Hash.hash(CommonParameter.getInstance()
                  .isECKeyCryptoEngine(), trx.getRawData().toByteArray()), approveList);
          tswBuilder.addAllApprovedList(approveList);
          tswBuilder.setCurrentWeight(currentWeight);
        }
```

**File:** chainbase/src/main/java/org/tron/core/capsule/TransactionCapsule.java (L597-645)
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

**File:** framework/src/test/java/org/tron/core/actuator/AccountPermissionUpdateActuatorTest.java (L951-971)
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
