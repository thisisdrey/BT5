### Title
Privilege escalation via `AccountPermissionUpdateContract`: an Active-permission key can seize full Owner control of an account - (File: `actuator/src/main/java/org/tron/core/actuator/AccountPermissionUpdateActuator.java`)

### Summary
`AccountPermissionUpdateActuator` lets whoever satisfies the signature-weight threshold of *any* permission referenced by `permissionId` on a transaction rewrite that account's entire `Owner`, `Witness`, and `Active` permission set — including the `Owner` permission itself — with no check that the new `Owner` permission retains any relationship to (or control by) the account's current legitimate owner. This mirrors the Rancher Global Roles flaw: a principal who is only supposed to hold a narrow delegated capability (here, an Active permission scoped to call `AccountPermissionUpdateContract`) can use that capability to grant itself unrestricted administrative (`Owner`) control.

### Finding Description
`AccountPermissionUpdateActuator.validate()` and its helper `checkPermission()` only validate the *structural* well-formedness of the submitted `owner`/`witness`/`actives` permissions (key count, threshold ≤ weight sum, distinct addresses, valid operations bitmap against `dynamicStore.getAvailableContractType()`), never against the caller's actual authority or the *existing* owner permission: [1](#0-0) 

The signer's authority to invoke the contract at all is checked separately and generically for every contract type in `TransactionCapsule.checkPermission`/`checkWeight`: it only requires that the signature weight under the permission referenced by `contract.getPermissionId()` meets that permission's own threshold, and — for non-owner permissions — that the permission's `operations` bitmap has the bit for `AccountPermissionUpdateContract` set: [2](#0-1) [3](#0-2) 

`checkPermission()` in the actuator validates the operations bitmap of a *new* Active permission only against the chain-wide `AVAILABLE_CONTRACT_TYPE` set, not against the caller's own currently-held operations: [4](#0-3) 

The codebase shows the developers were aware `AccountPermissionUpdateContract` is a dangerous capability: it is deliberately excluded from `ACTIVE_DEFAULT_OPERATIONS` (the default bitmap given to newly created accounts' Active permission), while still being present in `AVAILABLE_CONTRACT_TYPE` (the set `checkPermission` allows any Active permission to be granted): [5](#0-4) [6](#0-5) 

This confirms `AccountPermissionUpdateContract` is treated as a distinct, sensitive capability that account owners can nonetheless choose to delegate to an Active-permission key (e.g., to let an operations team member rotate keys or add signers). Once delegated, however, nothing in the actuator stops the holder of that Active permission from calling `AccountPermissionUpdateContract` to overwrite the `Owner` permission with a brand-new one under their sole control (threshold 1, single key = attacker's own address), permanently removing the original owner's key(s) from the account: [7](#0-6) 

The `execute()` path simply calls `account.updatePermissions(...)` with whatever was validated, with no restriction requiring the new owner keys to overlap with, or be authorized by, the pre-existing owner keys.

### Impact Explanation
Any account holder who delegates a limited Active permission that includes `AccountPermissionUpdateContract` in its `operations` bitmap (a legitimate, supported multisig administrative pattern) grants the holder of that key the ability to fully and permanently seize `Owner`-level control of the account — locking out the real owner and any other signers — and then use that Owner control (or a newly self-granted broad Active permission) to transfer out all TRX/TRC10/TRC20 balances, vote power, and resources. This is a full account takeover / theft-of-funds primitive reachable purely through a properly-signed transaction from an already-delegated but supposedly narrow-scope key, with no additional privileged access required. It matches CWE-269/CWE-285 (Improper Privilege Management/Authorization), directly analogous to the Rancher Global Roles CVE where possessing "edit" rights on the permission-defining object allowed unrestricted self-escalation.

### Likelihood Explanation
Exploitation requires only that some account owner has previously delegated an Active permission whose `operations` bitmap includes `AccountPermissionUpdateContract` to a third party (a supported and plausible multisig/administrative delegation pattern, since `AVAILABLE_CONTRACT_TYPE` permits it and nothing in documentation or code prevents an owner from doing so for convenience, e.g. "let this ops key rotate signers"). Once that precondition holds, the escalation itself requires a single signed `AccountPermissionUpdateContract` transaction from the delegated key — no race condition, no additional signatures, and no interaction with other subsystems.

### Recommendation
In `AccountPermissionUpdateActuator.validate()`, when the signer's authorizing permission (`contract.getPermissionId()`) is not the `Owner` permission (`permissionId != 0`), reject any update that modifies the `Owner` permission, or require that any new `Owner`/`Witness`/`Active` permission be a strict subset of the operations/authority already held by the invoking permission (mirroring how Rancher was patched to disallow granting Global Role permissions beyond what the editor already possesses). At minimum, disallow non-owner-authorized calls from altering the `Owner` permission's keys/threshold at all.

### Proof of Concept
1. Owner of account `A` creates a custom Active permission `P2` with `operations` bitmap including bit `AccountPermissionUpdateContract` (allowed, since `checkPermission` only checks against `AVAILABLE_CONTRACT_TYPE`), and delegates it (threshold 1) to key `K` for administrative convenience, via `AccountPermissionUpdateActuator`.
2. Attacker, controlling `K`, builds an `AccountPermissionUpdateContract` transaction with `permission_id = 2` (referencing `P2`), setting:
   - `owner` = new `Owner` permission with a single key = `K`, threshold = 1 (removing the original owner's key entirely).
   - `actives` = a new Active permission granting `K` all operations.
3. `K` signs the transaction; `TransactionCapsule.checkWeight`/`checkPermission` succeed because `K` meets `P2`'s threshold and `P2`'s operations include `AccountPermissionUpdateContract`.
4. `AccountPermissionUpdateActuator.validate()` passes (all structural checks on the new permissions succeed) and `execute()` overwrites `A`'s `Owner` permission to be solely controlled by `K`.
5. `K` now has full `Owner` control of account `A` and can transfer all funds/resources out, with the original owner permanently locked out.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/AccountPermissionUpdateActuator.java (L41-59)
```java
    AccountStore accountStore = chainBaseManager.getAccountStore();
    long fee = calcFee();
    final AccountPermissionUpdateContract accountPermissionUpdateContract;
    try {
      accountPermissionUpdateContract = any.unpack(AccountPermissionUpdateContract.class);

      byte[] ownerAddress = accountPermissionUpdateContract.getOwnerAddress().toByteArray();
      AccountCapsule account = accountStore.get(ownerAddress);
      account.updatePermissions(accountPermissionUpdateContract.getOwner(),
          accountPermissionUpdateContract.getWitness(),
          accountPermissionUpdateContract.getActivesList());
      accountStore.put(ownerAddress, account);

      adjustBalance(accountStore, ownerAddress, -fee);
      if (chainBaseManager.getDynamicPropertiesStore().supportBlackHoleOptimization()) {
        chainBaseManager.getDynamicPropertiesStore().burnTrx(fee);
      } else {
        adjustBalance(accountStore, accountStore.getBlackhole(), fee);
      }
```

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

**File:** chainbase/src/main/java/org/tron/core/capsule/TransactionCapsule.java (L233-270)
```java
  public static long checkWeight(Permission permission, List<ByteString> sigs, byte[] hash,
      List<ByteString> approveList)
      throws SignatureException, PermissionException, SignatureFormatException {
    long currentWeight = 0;
    if (sigs.size() > permission.getKeysCount()) {
      throw new PermissionException(
          "Signature count is " + (sigs.size()) + " more than key counts of permission : "
              + permission.getKeysCount());
    }
    HashMap addMap = new HashMap();
    for (ByteString sig : sigs) {
      if (sig.size() < 65) {
        throw new SignatureFormatException(
            "Signature size is " + sig.size());
      }
      String base64 = TransactionCapsule.getBase64FromByteString(sig);
      byte[] address = SignUtils
          .signatureToAddress(hash, base64, CommonParameter.getInstance().isECKeyCryptoEngine());
      long weight = getWeight(permission, address);
      if (weight == 0) {
        throw new PermissionException(
            ByteArray.toHexString(hash) + " is signed by " + encode58Check(address)
                + " but it is not contained of permission.");
      }
      if (ForkController.instance().pass(Parameter.ForkBlockVersionEnum.VERSION_4_7_1)) {
        base64 = encode58Check(address);
      }
      if (addMap.containsKey(base64)) {
        throw new PermissionException(encode58Check(address) + " has signed twice!");
      }
      addMap.put(base64, weight);
      if (approveList != null) {
        approveList.add(ByteString.copyFrom(address)); //out put approve list.
      }
      currentWeight += weight;
    }
    return currentWeight;
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

**File:** chainbase/src/main/java/org/tron/core/store/DynamicPropertiesStore.java (L681-695)
```java
    try {
      this.getAvailableContractType();
    } catch (IllegalArgumentException e) {
      String contractType = "7fff1fc0037e0000000000000000000000000000000000000000000000000000";
      byte[] bytes = ByteArray.fromHexString(contractType);
      this.saveAvailableContractType(bytes);
    }

    try {
      this.getActiveDefaultOperations();
    } catch (IllegalArgumentException e) {
      String contractType = "7fff1fc0033e0000000000000000000000000000000000000000000000000000";
      byte[] bytes = ByteArray.fromHexString(contractType);
      this.saveActiveDefaultOperations(bytes);
    }
```

**File:** framework/src/test/java/org/tron/core/actuator/AccountPermissionUpdateActuatorTest.java (L951-977)
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

  }
```
