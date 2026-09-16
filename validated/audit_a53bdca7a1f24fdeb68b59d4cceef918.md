### Title
Account owner can permanently lock itself out via `AccountPermissionUpdateContract` with no safeguard against unreachable permissions - ([File: actuator/src/main/java/org/tron/core/actuator/AccountPermissionUpdateActuator.java])

### Summary
The reported Vyper issue is a design flaw where a privileged role (`is_operator`) can be freely added/removed with no protection against the last/only privileged actor removing itself, permanently disabling all privileged functionality. The same architectural flaw exists in java-tron's account multi-sig permission system: `AccountPermissionUpdateContract`/`AccountPermissionUpdateActuator` lets an account owner replace its own `owner`, `witness`, and `active` `Permission` structures in a single transaction with no check that the new configuration remains satisfiable by keys the sender actually controls, and no protection preventing the owner from irreversibly disabling further permission updates for that account.

### Finding Description
`AccountPermissionUpdateActuator.execute()` simply calls `account.updatePermissions(owner, witness, actives)` and persists the new permission set, unconditionally overwriting the previous `ownerPermission`, `witnessPermission`, and `activePermission` fields. [1](#0-0) 

`validate()` and the helper `checkPermission()` only check structural constraints of the submitted `Permission` objects — key count bounds, distinct addresses, positive weights/threshold, weight-sum ≥ threshold, name length, valid contract-type bitmap for `Active` permissions, etc. Nowhere is it verified that the transaction sender (or any real-world key holder) actually controls a combination of the new keys sufficient to reach the new threshold, nor is there any restriction preventing the new `owner` permission from excluding all previously-valid keys. [2](#0-1) [3](#0-2) 

`AccountCapsule.updatePermissions()` performs an unconditional overwrite of the owner permission (and, when applicable, witness/active permissions) with whatever was submitted: [4](#0-3) 

This mirrors exactly the flaw described in the report: `is_operator` self-assignment/self-removal with no "main operator" guard rail allowing the sole privileged actor to disable all privileged operations on the contract. In java-tron, the account owner (an otherwise unprivileged, ordinary signer — no SR/witness/committee role required) can, in one signed transaction, set an `owner` permission whose keys/threshold cannot be satisfied by any key it still controls, or omit its own controlling key entirely. Because `AccountPermissionUpdateContract` itself requires the current `owner` permission's signature threshold to authorize, once the owner permission is replaced with an unreachable configuration, no further `AccountPermissionUpdateContract` (or any other owner-permission-gated action) can ever be authorized for that account again.

### Impact Explanation
A single self-issued transaction can permanently and irrevocably freeze an account: no future transfers, resource operations, or permission corrections gated by the `Owner` (or `Active`) permission can be authorized once the permission set becomes unsatisfiable. This matches the accepted impact class of "permanent freezing of funds," since all TRX/TRC10/resources held by that account become permanently inaccessible with no recovery path, exactly as in the original report where a contract's privileged surface becomes permanently unusable once the last operator removes itself.

### Likelihood Explanation
Likelihood of accidental self-lockout is realistic given the complexity of correctly constructing `owner`/`witness`/`active` `Permission` protobuf messages (weights, thresholds, key sets) via `AccountPermissionUpdateContract`, and there is no validation-time or execution-time safeguard (e.g., simulate-signature-reachability check) to reject an unreachable permission set before committing it on-chain. This is directly reachable by any ordinary account (once multi-sign is enabled via `AllowMultiSign`), through the standard JSON-RPC/HTTP path (`AccountPermissionUpdateServlet`) into the actuator. [5](#0-4) 

### Recommendation
Add a validation step in `AccountPermissionUpdateActuator.validate()` (or `checkPermission()`) that rejects new `Owner`/`Active` permission sets where the submitting transaction's signature set no longer satisfies the new threshold, or otherwise require a "recovery" mechanism analogous to the report's suggested `is_main_operator` guard so the account can never be left with an unreachable owner permission.

### Proof of Concept
1. Enable multi-sign for an account (`AllowMultiSign == 1`).
2. Submit `AccountPermissionUpdateContract` with a valid current-owner signature, setting the new `owner` `Permission` to a set of keys/threshold that the sender does not control (or a threshold higher than the combined weight of any keys it controls).
3. `AccountPermissionUpdateActuator.validate()` passes all structural checks in `checkPermission()` and `execute()` commits the new permission via `AccountCapsule.updatePermissions()`.
4. All subsequent transactions requiring `Owner`/`Active` authorization for that account (including further `AccountPermissionUpdateContract` attempts to fix the mistake) fail signature verification permanently, freezing the account's funds and resources.

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

**File:** actuator/src/main/java/org/tron/core/actuator/AccountPermissionUpdateActuator.java (L71-122)
```java
  private boolean checkPermission(Permission permission) throws ContractValidateException {
    DynamicPropertiesStore dynamicStore = chainBaseManager.getDynamicPropertiesStore();
    if (permission.getKeysCount() > dynamicStore.getTotalSignNum()) {
      throw new ContractValidateException("number of keys in permission should not be greater "
          + "than " + dynamicStore.getTotalSignNum());
    }
    if (permission.getKeysCount() == 0) {
      throw new ContractValidateException("key's count should be greater than 0");
    }
    if (permission.getType() == PermissionType.Witness && permission.getKeysCount() != 1) {
      throw new ContractValidateException("Witness permission's key count should be 1");
    }
    if (permission.getThreshold() <= 0) {
      throw new ContractValidateException("permission's threshold should be greater than 0");
    }
    String name = permission.getPermissionName();
    if (!StringUtils.isEmpty(name) && name.length() > 32) {
      throw new ContractValidateException("permission's name is too long");
    }
    //check owner name ?
    if (permission.getParentId() != 0) {
      throw new ContractValidateException("permission's parent should be owner");
    }

    long weightSum = 0;
    List<ByteString> addressList = permission.getKeysList()
        .stream()
        .map(x -> x.getAddress())
        .distinct()
        .collect(toList());
    if (addressList.size() != permission.getKeysList().size()) {
      throw new ContractValidateException(
          "address should be distinct in permission " + permission.getType());
    }
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
    if (weightSum < permission.getThreshold()) {
      throw new ContractValidateException(
          "sum of all key's weight should not be less than threshold in permission " + permission
              .getType());
    }
```

**File:** actuator/src/main/java/org/tron/core/actuator/AccountPermissionUpdateActuator.java (L187-228)
```java
    if (!accountPermissionUpdateContract.hasOwner()) {
      throw new ContractValidateException("owner permission is missed");
    }

    if (accountCapsule.getIsWitness()) {
      if (!accountPermissionUpdateContract.hasWitness()) {
        throw new ContractValidateException("witness permission is missed");
      }
    } else {
      if (accountPermissionUpdateContract.hasWitness()) {
        throw new ContractValidateException("account isn't witness can't set witness permission");
      }
    }

    if (accountPermissionUpdateContract.getActivesCount() == 0) {
      throw new ContractValidateException("active permission is missed");
    }
    if (accountPermissionUpdateContract.getActivesCount() > 8) {
      throw new ContractValidateException("active permission is too many");
    }

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

**File:** chainbase/src/main/java/org/tron/core/capsule/AccountCapsule.java (L1301-1320)
```java
  public void updatePermissions(Permission owner, Permission witness, List<Permission> actives) {
    Builder builder = this.account.toBuilder();

    owner = owner.toBuilder().setId(0).build();
    builder.setOwnerPermission(owner);
    if (witness != null && builder.getIsWitness()) {
      witness = witness.toBuilder().setId(1).build();
      builder.setWitnessPermission(witness);
    }

    builder.clearActivePermission();
    if (actives != null) {
      for (int i = 0; i < actives.size(); i++) {
        Permission permission = actives.get(i).toBuilder().setId(i + 2).build();
        builder.addActivePermission(permission);
      }
    }

    this.account = builder.build();
  }
```

**File:** framework/src/main/java/org/tron/core/services/http/AccountPermissionUpdateServlet.java (L26-37)
```java
  protected void doPost(HttpServletRequest request, HttpServletResponse response) {
    try {
      PostParams params = PostParams.getPostParams(request);
      AccountPermissionUpdateContract.Builder build = AccountPermissionUpdateContract.newBuilder();
      JsonFormat.merge(params.getParams(), build, params.isVisible());

      Transaction tx = wallet
          .createTransactionCapsule(build.build(), ContractType.AccountPermissionUpdateContract)
          .getInstance();
      JSONObject jsonObject = JSONObject.parseObject(params.getParams());
      tx = Util.setTransactionPermissionId(jsonObject, tx);
      response.getWriter().println(Util.printCreateTransaction(tx, params.isVisible()));
```
