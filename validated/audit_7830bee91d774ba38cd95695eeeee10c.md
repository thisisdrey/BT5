### Title
Missing anti-lockout validation in `AccountPermissionUpdateActuator` allows permanent freezing of all account funds - (File: `actuator/src/main/java/org/tron/core/actuator/AccountPermissionUpdateActuator.java`)

### Summary
This is analogous to the escrow bug where a state-machine misconfiguration (no arbiter) creates a path with no way back, permanently locking user funds. In java-tron's TRON multi-signature model, `AccountPermissionUpdateContract` (reachable directly by any unprivileged account holder via a signed transaction) lets an account owner replace its `Owner`, `Witness`, and `Active` permissions. The actuator's `checkPermission()` only validates structural properties of the new permission (key count bounds, distinct addresses, weight > 0, `threshold > 0`, `weightSum >= threshold`) but never verifies that the new `Owner` permission's keys are actually controllable/reachable by any real signer that can later re-authorize a transaction.

### Finding Description
`checkPermission()` in `AccountPermissionUpdateActuator.java` performs only syntactic checks: [1](#0-0) 

Nothing in `validate()` or `execute()` checks that the resulting `Owner` permission remains satisfiable by the caller or by any key whose private key is actually held by someone. `execute()` then unconditionally overwrites the account's permissions via `account.updatePermissions(...)`: [2](#0-1) 

Because the `Owner` permission is the only permission type authorized to submit a future `AccountPermissionUpdateContract` (multi-sig transactions are authorized against `Permission.keys`/`threshold`, verified elsewhere against the account's stored permissions), once an account sets its `Owner` permission keys to addresses that nobody can produce valid signatures for (e.g., randomly generated addresses, a burn/blackhole-style address, or a combination whose combined weight can never be reproduced), there is no subsequent transaction — including a corrective `AccountPermissionUpdateContract` — that can ever satisfy the new `Owner` threshold again. This mirrors the escrow bug's core defect: a party can drive the system into a state (no valid arbiter / no valid signer) from which the protocol provides no exit path, and all assets tied to that account (TRX balance, TRC10/TRC20 holdings, frozen/staked balances, votes, delegated resources) become permanently unreachable.

### Impact Explanation
Any account (including exchange hot wallets, contract-controlling accounts, or DAO-style multisig accounts) that misconfigures its `Owner` permission — either by mistake or via a compromised/malicious co-signer proposing a permission update that other signers approve without realizing the resulting threshold is unreachable — permanently loses control of every asset associated with that account. This is a permanent freezing-of-funds condition with no protocol-level recovery mechanism, matching the "permanent freezing of funds" impact class validated for this report.

### Likelihood Explanation
The transaction is fully reachable by any unprivileged account holder using a single signed `AccountPermissionUpdateContract` — no special privilege, arbiter, committee, or SR access is required, only that `AllowMultiSign` is enabled (already the case on mainnet). Multi-sig setups with several co-signers are the most exposed, since a single confused/malicious signer's proposed permission update, once it collects enough approvals to hit its own (potentially unreachable) threshold, permanently bricks the account without any of the approvers necessarily realizing it. The likelihood of accidental triggering (e.g., typo'd key addresses, wrong threshold math) is realistic given no validation guards against it.

### Recommendation
Add a validation step in `AccountPermissionUpdateActuator.checkPermission()` (specifically for the `Owner` permission) that rejects updates which the actuator cannot at least sanity-check as being satisfiable, e.g.:
- Require that the caller's currently valid signing key(s) that authorized the current transaction remain part of the new `Owner` permission with sufficient combined weight to meet the new threshold, or
- Provide a mandatory "self-inclusion" check ensuring `ownerAddress` itself (or a key it directly controls) is present with adequate weight, or
- At minimum, warn/require explicit acknowledgment flags for high-risk configurations (e.g., threshold unreachable by any single previously-authorized signer combination) before committing the change.

### Proof of Concept
1. Account `A` has `AllowMultiSign` enabled and calls `AccountPermissionUpdateContract` to set its `Owner` permission to `{keys: [random_address_X (weight 1), random_address_Y (weight 1)], threshold: 2}`, where neither `X` nor `Y`'s private key is known to `A` or anyone else.
2. `checkPermission()` accepts this because `keysCount > 0`, addresses are distinct and valid, weights `> 0`, `threshold (2) > 0`, and `weightSum (2) >= threshold (2)`.
3. `execute()` commits the new `Owner` permission via `account.updatePermissions(...)`.
4. From this point forward, no transaction requiring `Owner`-level authorization (including a corrective `AccountPermissionUpdateContract`) can ever be signed, because no one holds valid signatures for `X` or `Y`. All of account `A`'s TRX, TRC10/TRC20 tokens, frozen/staked balances, and voting power become permanently inaccessible.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/AccountPermissionUpdateActuator.java (L43-59)
```java
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
