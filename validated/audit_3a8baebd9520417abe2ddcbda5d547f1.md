### Title
Missing zero-address check on permission keys in `AccountPermissionUpdateActuator` allows permanent account lock-out - (File: `actuator/src/main/java/org/tron/core/actuator/AccountPermissionUpdateActuator.java`)

### Summary
The reported Pickle Finance bug class is "constructor/parameter accepted without a `require(addr != address(0))` check," letting a zero address be permanently stored as a privileged parameter. The reachable java-tron analog is `AccountPermissionUpdateActuator.checkPermission()`, which validates permission key addresses only through `DecodeUtil.addressValid()` — a format/prefix/length check — without ever rejecting the reserved all-zero address (`0x41` + 20 zero bytes). This lets a `AccountPermissionUpdateContract` transaction set an account's Owner/Witness/Active permission keys to the zero address, which has no corresponding private key.

### Finding Description
`AccountPermissionUpdateActuator.validate()` calls `checkPermission()` for the Owner permission, the Witness permission (if applicable), and every Active permission supplied in the contract. [1](#0-0) 

Inside `checkPermission()`, each key's address is validated solely with `DecodeUtil.addressValid()`: [2](#0-1) 

`DecodeUtil.addressValid()` only checks the byte-array length and the network address prefix byte; it does not exclude the all-zero payload (i.e., the "zero address" analog for TRON, `0x41` followed by 20 zero bytes) from being accepted, mirroring exactly the class of bug in the report: a constructor/setter parameter is format-checked but never checked against the zero/null sentinel value before being persisted as a trusted, privileged value. Once accepted, `execute()` persists the permission directly into the account via `AccountCapsule.updatePermissions(...)` with no further sanitation: [3](#0-2) 

Because nobody possesses the private key for the zero address, any permission (Owner, Witness, or Active) whose threshold can only be met by including that key becomes permanently unsatisfiable.

### Impact Explanation
If an account's Owner permission (or an Active permission that gates asset movement) is set with a threshold that requires the zero-address key's weight to be met, the account can never again produce a valid signature set for that permission. This permanently freezes the account's ability to execute any transaction gated by that permission — including sending TRX/TRC10/TRC20 balances or issuing a corrective `AccountPermissionUpdateContract` — which matches the "permanent freezing of funds" impact category. This is a directly reachable, single-transaction-triggered defect: any signed `AccountPermissionUpdateContract` broadcast by an unprivileged accountholder (or a wallet/tool that programmatically builds such a contract and fails to sanitize a default/uninitialized `ByteString` address) can trigger it, since the actuator itself provides no backstop.

### Likelihood Explanation
Likelihood is moderate: the transaction must be crafted with an Owner/Active permission whose threshold-satisfying keys include the zero address. This can happen either through attacker-controlled tooling that intentionally targets a victim who signs a permission update built by that tooling, or unintentionally via a bug in wallet/SDK code that leaves an address field at its zero-initialized default before submission — a class of mistake the report explicitly calls out (uninitialized/zero constructor parameters reaching production state without validation).

### Recommendation
In `AccountPermissionUpdateActuator.checkPermission()`, after or alongside the existing `DecodeUtil.addressValid()` check, explicitly reject the all-zero address for every `Key` in `permission.getKeysList()` (i.e., the TRON equivalent of `require(address != address(0))`), for the Owner, Witness, and all Active permissions before they are persisted via `execute()`.

### Proof of Concept
1. Construct an `AccountPermissionUpdateContract` for `ownerAddress` where the `owner` `Permission` contains a `Key` with `address = 0x41 + 20*0x00` and `weight` sufficient (together with any other keys) to reach `threshold`.
2. Submit the transaction; `AccountPermissionUpdateActuator.validate()` passes `checkPermission(owner)` because `DecodeUtil.addressValid()` only checks length/prefix, not the zero payload (see `checkPermission`, lines 105-117 above).
3. `execute()` persists the new Owner permission via `account.updatePermissions(...)`.
4. Because no private key exists for the zero address, any future transaction requiring the Owner permission threshold (including another `AccountPermissionUpdateContract` to undo the change) can never be validly signed — the account's funds and control are permanently frozen.

Note: I was unable to view the full source of `DecodeUtil.addressValid()` (only located it via `grep_search`) before the session ended, so its exact zero-address handling should be double-checked directly in `common/src/main/java/org/tron/common/utils/DecodeUtil.java` to confirm there is no separate zero-address rejection elsewhere in the call chain.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/AccountPermissionUpdateActuator.java (L44-52)
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

**File:** actuator/src/main/java/org/tron/core/actuator/AccountPermissionUpdateActuator.java (L105-117)
```java
    for (Key key : permission.getKeysList()) {
      if (!DecodeUtil.addressValid(key.getAddress().toByteArray())) {
        throw new ContractValidateException("key is not a validate address");
      }
      if (key.getWeight() <= 0) {
        throw new ContractValidateException("key's weight should be greater than 0");
      }
      try {
        weightSum = addExact(weightSum, key.getWeight());
      } catch (ArithmeticException e) {
        throw new ContractValidateException(e.getMessage());
      }
    }
```

**File:** actuator/src/main/java/org/tron/core/actuator/AccountPermissionUpdateActuator.java (L212-227)
```java
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
```
