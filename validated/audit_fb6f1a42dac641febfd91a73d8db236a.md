This is a genuine, allowed privilege-escalation path in java-tron's Active-permission model, and it maps directly onto the "hollow protocol / god key" bug class from the report: an owner can (intentionally or by mistake) grant an "Active" (non-Owner) key the ability to invoke `AccountPermissionUpdateContract`, letting that supposedly limited key re-write the account's `Owner` permission and hijack the account, exactly like TesseraDAO's single admin key controlling role assignment, ownership transfer, minting and withdrawal.

Key mechanics found in the codebase:

- `AccountPermissionUpdateActuator.checkPermission()` validates an `Active` permission's `operations` bitmap only against `dynamicStore.getAvailableContractType()` — it does **not** exclude `AccountPermissionUpdateContract` itself from the set of contract types an Active permission is allowed to authorize. [1](#0-0) 
- The only enforcement that a low-privilege ("Active") key cannot act as owner happens generically in `TransactionUtil.checkPermissionOperations()` / `TransactionCapsule` permission checks, which simply test whether the bit for the contract's `ContractType` is set in the signer's `Permission.operations` bitmap — there is no special-case rejection of `AccountPermissionUpdateContract` for non-owner (`permissionId != 0`) signers. [2](#0-1) [3](#0-2) 
- `AccountPermissionUpdateActuator.execute()` then unconditionally overwrites the account's `Owner`, `Witness`, and `Active` permissions with whatever was supplied in the contract, with no re-check that the new owner permission still includes the original account holder, and no check that the caller's own permission id is the Owner permission (id 0). [4](#0-3) 

Consequence: if any account owner (by mistake, social-engineering, or a poorly-designed "operator/admin" active-key setup — analogous to TesseraDAO handing broad function access to a single key) issues an Active permission whose 32-byte `operations` bitmap includes the bit for `AccountPermissionUpdateContract`, that Active key — which was meant to be scoped to specific operations (e.g., transfers, asset trading) — can single-handedly submit an `AccountPermissionUpdateContract` transaction that replaces the `Owner` permission with keys it controls, and thereby seize total account control (mint/transfer assets, freeze/vote resources, trade, withdraw) exactly as the compromised "god key" did in the TesseraDAO incident. This is reachable by any unprivileged account owner who configures such a permission (self-inflicted centralization) or by an attacker who compromises only the Active key rather than the Owner key.

### Title
Active permission can be granted rights to execute `AccountPermissionUpdateContract`, enabling non-owner key to seize full account ownership - ([File: actuator/src/main/java/org/tron/core/actuator/AccountPermissionUpdateActuator.java])

### Summary
`AccountPermissionUpdateActuator.checkPermission()` does not forbid `AccountPermissionUpdateContract` from being included in an `Active` permission's allowed-operations bitmap, and `execute()` blindly overwrites the account's Owner/Witness/Active permissions supplied by the caller. Any account holder who (deliberately or by misconfiguration) grants a lower-privilege Active key the `AccountPermissionUpdateContract` operation bit effectively creates a single "god key" that can rewrite Owner permission and take over the entire account, mirroring the centralized-admin-key failure described in the TesseraDAO report.

### Finding Description
Tron's multi-sig permission model is intended to segregate an `Owner` permission (full control) from scoped `Active` permissions (limited to specific `ContractType`s via a 256-bit `operations` bitmap). `AccountPermissionUpdateActuator.checkPermission()` validates an Active permission's operations bitmap purely against the set of globally available contract types (`dynamicStore.getAvailableContractType()`), with no denylist preventing the `AccountPermissionUpdateContract` type itself from being authorized under an Active permission. [5](#0-4) 

At transaction-processing time, the enforcement of which permission id may sign which contract type is generic: `TransactionUtil.checkPermissionOperations()` (and the equivalent path in `TransactionCapsule.checkPermission()`) merely checks the operation bit for the contract's type against the signer's permission bitmap — it treats `AccountPermissionUpdateContract` like any other operation. [2](#0-1) [3](#0-2) 

Once such a transaction passes signature/permission checks, `execute()` calls `account.updatePermissions(...)` and persists it, replacing Owner, Witness and Active permissions wholesale, with no validation that the resulting Owner permission still includes any key the original account holder controls. [4](#0-3) 

This mirrors TesseraDAO's root cause: a single key held broad, unreviewed authority (mint, ownership transfer, trade, withdraw) with "no delay, no second signature, no circuit breaker between the command and execution." In java-tron, the analogous condition is that an Active key — intended to be scoped — can be handed (or can retain, if never revoked/rotated) the specific capability to rewrite the account's own Owner permission, collapsing the separation the multi-sig design is meant to provide.

### Impact Explanation
An account whose owner delegates an Active permission that includes `AccountPermissionUpdateContract` in its bitmap (e.g., an "admin operator" key meant only for asset/trading operations) grants that key the ability to unilaterally take over the account: reassign Owner permission to attacker-controlled addresses, then use the new Owner authority to transfer TRX/TRC10 assets, vote/freeze resources, or otherwise fully control the account — a direct "unauthorized account operation, theft of funds" outcome matching the report's mint/reassign-role/withdraw sequence. Because Owner-permission takeover cascades into every other actuator's authorization checks, the blast radius is total account compromise, not limited to a single operation.

### Likelihood Explanation
Exploitation requires only that some account owner has previously issued an `AccountPermissionUpdateContract` transaction that (knowingly or through UI/tooling error) sets an Active permission's operations bitmap to include the `AccountPermissionUpdateContract` bit. Given that `checkAvailableContractTypeCorrespondingToCode` shows the default "available contract type" bitmap does include many bits and no code path excludes this specific bit from Active permissions, this is a configuration hazard rather than requiring any additional signature bypass — once misconfigured, exploitation is a single ordinary signed transaction from the Active key. [6](#0-5) 

### Recommendation
In `AccountPermissionUpdateActuator.checkPermission()`, explicitly disallow the `AccountPermissionUpdateContract` bit (and any other permission-management contract types) from being set in `Active` permission operation bitmaps, so only the `Owner` permission (id 0) can ever authorize changes to the account's own permission structure. Additionally, consider validating in `execute()`/`validate()` that the new `Owner` permission is only settable by a transaction actually signed under permission id 0.

### Proof of Concept
1. Account `A` (Owner key `K_owner`) issues `AccountPermissionUpdateContract` creating an `Active` permission `P1` with key `K_admin` and an `operations` bitmap that includes bit `ContractType.AccountPermissionUpdateContract_VALUE` (in addition to legitimate bits like `TransferContract`), following the exact validation path in `AccountPermissionUpdateActuator.checkPermission()` which only rejects operations bits not present in `getAvailableContractType()`, not this specific bit. [1](#0-0) 
2. `K_admin`, using permission id referencing `P1`, submits a new `AccountPermissionUpdateContract` transaction that sets `Owner` permission to a key `K_attacker` it controls, satisfying `checkPermissionOperations` (bit set) and `checkPermission`/threshold checks in `TransactionCapsule`. [2](#0-1) 
3. `execute()` persists the new Owner permission unconditionally, giving `K_attacker` full account control, after which it can transfer all assets/TRX out of account `A`. [4](#0-3)

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/AccountPermissionUpdateActuator.java (L44-53)
```java
    try {
      accountPermissionUpdateContract = any.unpack(AccountPermissionUpdateContract.class);

      byte[] ownerAddress = accountPermissionUpdateContract.getOwnerAddress().toByteArray();
      AccountCapsule account = accountStore.get(ownerAddress);
      account.updatePermissions(accountPermissionUpdateContract.getOwner(),
          accountPermissionUpdateContract.getWitness(),
          accountPermissionUpdateContract.getActivesList());
      accountStore.put(ownerAddress, account);

```

**File:** actuator/src/main/java/org/tron/core/actuator/AccountPermissionUpdateActuator.java (L124-146)
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

**File:** framework/src/test/java/org/tron/core/actuator/AccountPermissionUpdateActuatorTest.java (L919-943)
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
```
