### Title
Single-step, irrevocable account permission (ownership) update in `AccountPermissionUpdateContract` can lead to permanent loss of account control - (File: actuator/src/main/java/org/tron/core/actuator/AccountPermissionUpdateActuator.java)

### Summary
The `AccountPermissionUpdateContract` mechanism in java-tron lets an account owner atomically overwrite its `Owner`, `Witness` and `Active` permissions in a single transaction. There is no two-step "propose-then-accept" process analogous to what the external report recommends for `transferOwnership`. Once `AccountPermissionUpdateActuator.execute()` commits the new `Owner` permission, the change is immediate and irreversible — there is no mechanism for the new key(s) to confirm they control the address before the old owner permission is discarded.

### Finding Description
`AccountPermissionUpdateActuator.execute()` unpacks the `AccountPermissionUpdateContract` and immediately calls `AccountCapsule.updatePermissions()`, which overwrites the account's `OwnerPermission` (and Witness/Active permissions) in the same step: [1](#0-0) 

The corresponding capsule mutation directly replaces the stored owner permission with the new one supplied in the transaction, with no staging or pending-acceptance state: [2](#0-1) 

`validate()` only checks structural correctness of the new permission (key count, weight sum vs. threshold, address format via `DecodeUtil.addressValid`) — it never verifies that the new key holder(s) actually control the specified address or that they consent to becoming the new owner/active signer: [3](#0-2) 

This is reachable by any unprivileged account holder via the standard broadcast path: build an `AccountPermissionUpdateContract`, sign it with the current owner permission keys, and broadcast — exposed through `AccountPermissionUpdateServlet`/gRPC `Wallet.createTransactionCapsule`: [4](#0-3) 

This mirrors exactly the bug class described in the report: a single, unconfirmed step permanently changes who controls the account (analogous to `Ownable.transferOwnership`), with no way for the new key holder to "accept" and no way to recover from a typo or key-generation mistake before the change becomes binding.

### Impact Explanation
If a signer mistypes an address, uses a key they do not actually control, or misconfigures the threshold/weights when submitting `AccountPermissionUpdateContract` (e.g., raising the `Owner` permission threshold above the achievable weight sum from keys they hold, or assigning owner/active keys to an address whose private key is lost/unavailable), the account's TRX, TRC10, and TRC20 balances, frozen/staked resources, and voting rights become permanently unreachable — no privileged or protocol-level recovery path exists once the transaction is confirmed on-chain. This is a concrete permanent freezing-of-funds condition triggered entirely by an ordinary, unprivileged account holder's own transaction.

### Likelihood Explanation
Likelihood is driven by user/operational error rather than an attacker, but the report's own framing establishes this as a legitimate risk class worth flagging: any account using multi-sig/permission management (a normal, supported java-tron feature) can trigger this with a single malformed `AccountPermissionUpdateContract` transaction — no attacker collusion or special privilege required, only a mistake by the account's own current owner-permission signer(s).

### Recommendation
- **Short term**: Introduce a two-phase process for `AccountPermissionUpdateContract`-driven owner/active permission changes: stage the proposed new permission set, and require a subsequent transaction signed under the *new* keys (or an explicit acceptance transaction) before `AccountCapsule.updatePermissions()` commits the change and discards the old permission.
- **Long term**: Document all privileged/self-privileged state transitions reachable via actuators (`AccountPermissionUpdateActuator`, `UpdateBrokerageActuator`, `UpdateAssetActuator`, etc.) that irreversibly alter control of account resources, and audit for similar "single transaction, no confirmation" patterns.

### Proof of Concept
1. Account `A` currently has default owner/active permissions matching its own key.
2. `A` broadcasts an `AccountPermissionUpdateContract` setting `Owner` permission's key list to an address `B` for which `A` does not actually hold the private key (typo, copy-paste error, or unowned vanity address), with `threshold` satisfied only by that key.
3. `AccountPermissionUpdateActuator.validate()` passes since it only checks format/weight-sum consistency, not key custody: [5](#0-4) 
4. `execute()` commits `updatePermissions()`, replacing the account's owner permission irreversibly.
5. Account `A`'s owner/active control is now permanently unusable — no follow-up transaction can restore it, and no acceptance step from `B` was ever required.

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

**File:** actuator/src/main/java/org/tron/core/actuator/AccountPermissionUpdateActuator.java (L178-221)
```java
    byte[] ownerAddress = accountPermissionUpdateContract.getOwnerAddress().toByteArray();
    if (!DecodeUtil.addressValid(ownerAddress)) {
      throw new ContractValidateException("invalidate ownerAddress");
    }
    AccountCapsule accountCapsule = accountStore.get(ownerAddress);
    if (accountCapsule == null) {
      throw new ContractValidateException("ownerAddress account does not exist");
    }

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

**File:** framework/src/main/java/org/tron/core/services/http/AccountPermissionUpdateServlet.java (L22-41)
```java
  protected void doGet(HttpServletRequest request, HttpServletResponse response) {

  }

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
    } catch (Exception e) {
      Util.processError(e, response);
    }
  }
```
