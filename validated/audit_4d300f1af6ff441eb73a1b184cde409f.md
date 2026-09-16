## Analysis: TVM `ValidateMultiSign` Precompile Skips the Permission-Type (Role) Check Enforced Everywhere Else

The PicketLink CVE is a "role-based authorization not properly checked" bug: one code path (SP-initiated flow) enforced role/permission checks, while another reachable path (direct request) did not. java-tron has a structurally identical inconsistency between how it enforces the `Active`-only restriction on permission IDs.

### Title
Missing permission-type (role) check in TVM `ValidateMultiSign` precompile allows Witness-permission keys to satisfy authorization checks reserved for Active permissions - (File: `actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java`)

### Summary
Every transaction-level permission enforcement path in java-tron requires that a non-owner `permissionId` refer to a permission of type `Active` — Witness-type permission (id=1) is explicitly rejected for use in authorizing anything other than block production. The `ValidateMultiSign` TVM precompile, which exposes the exact same weight/threshold verification primitive to smart contracts, omits this type check entirely.

### Finding Description
`TransactionCapsule.checkPermission` enforces the rule consistently for real transactions: [1](#0-0) 

The same rule is duplicated at the query/estimation surfaces (`TransactionUtil.getTransactionSignWeight` and `Wallet.getTransactionApprovedList`): [2](#0-1) [3](#0-2) 

In every one of these, `permission.getType() != PermissionType.Active` throws a `PermissionException`, i.e., a Witness (id=1) or malformed permission can never be used to authorize an arbitrary contract/operation.

However, the `ValidateMultiSign` precompile — reachable from any deployed smart contract via a `STATICCALL`/`CALL` to its fixed address, and thus by any unprivileged contract-deployer or caller — performs the identical weight/threshold computation but never checks `permission.getType()`: [4](#0-3) 

It simply does `account.getPermissionById(permissionId)` and compares `totalWeight >= permission.getThreshold()`. If `permissionId == 1`, `AccountCapsule.getPermissionById` will happily return the account's **Witness** permission: [5](#0-4) 

Per protocol design (see the protocol doc and `AccountPermissionUpdateActuator.checkPermission`), the Witness permission is meant to hold exactly one key and is used solely for block-header signing (`BlockCapsule.validateSignature` reads `getWitnessPermissionAddress()` directly, bypassing the general permission-operations system entirely): [6](#0-5) [7](#0-6) 

Because the Witness key is used for automated, high-frequency block signing, it is operationally a "hotter" key with materially different security custody than the Active/Owner keys used to authorize asset movement. Every other place in the codebase that lets a `permissionId` gate an operation deliberately excludes this key from general-purpose authorization. `ValidateMultiSign` — the one precompile explicitly built for smart contracts (e.g., custody/bridge/DAO multisig contracts) to verify "this address approved this data" — does not, letting that same lower-assurance key silently satisfy checks intended to require the higher-assurance Active permission.

### Impact Explanation
Contracts on TRON (bridges, custodial wallets, DAO/multisig implementations) commonly use `ValidateMultiSign` as their on-chain authorization primitive instead of re-implementing ECDSA recovery, trusting that java-tron enforces the same "Active-only" rule it enforces for ordinary transactions. Because that assumption is violated here, an attacker who compromises or otherwise obtains a witness's block-signing key (a key that must be kept online/automatable for continuous block production and is therefore a materially weaker security boundary than the account's active/owner keys) can forge a passing `ValidateMultiSign` call for that witness's account by supplying `permissionId = 1`. Any contract-level access control gated on this precompile for that account can then be bypassed, enabling unauthorized approval of withdrawals/transfers or other privileged smart-contract actions — a concrete unauthorized-operation / fund-theft risk consistent with the required impact bar.

### Likelihood Explanation
The precompile is callable by any contract deployer/caller with no special privilege, matching the "unprivileged contract deployer/caller" reachability requirement. The only precondition is that the target account is a witness (so a Witness permission exists) and that some contract relies on `ValidateMultiSign` for identity/authorization of that account — a documented, real-world TRON usage pattern. No fork/feature flag gates this code path (unlike many other precompiles gated behind `VMConfig.allowTvm*`).

### Recommendation
Add the same `permission.getType() != PermissionType.Active` (or `permissionId == 0` for Owner) rejection inside `PrecompiledContracts.ValidateMultiSign.execute()` that already exists in `TransactionCapsule.checkPermission`, so the precompile cannot be satisfied using a Witness-type permission.

### Proof of Concept
1. Account `A` is a Super Representative (witness) with owner-set Witness permission key `Kw` (used only for block signing) and a separate Active permission key `Ka` (id=2, intended for general authorization).
2. A third-party contract `C` implements `withdraw(bytes data, bytes[] sigs)` and gates it by calling the `ValidateMultiSign` precompile with `(A, 2, hash(data), sigs)` intending to require `Ka`-signed approval.
3. If instead `sigs` are signed by `Kw` and the caller passes `permissionId = 1`, `PrecompiledContracts.ValidateMultiSign.execute` (lines 1080-1111) fetches the Witness permission via `account.getPermissionById(1)`, computes `totalWeight` from `Kw`'s signature, and returns `true` once `totalWeight >= permission.getThreshold()` (threshold 1 for a single-key Witness permission) — with no check that this permission is of type `Active`, unlike every transaction-level check (`TransactionCapsule.checkPermission`, `TransactionUtil.getTransactionSignWeight`, `Wallet.getTransactionApprovedList`) which would reject `permissionId=1` outright.
4. Contract `C`'s `withdraw` logic is thus satisfied by a signature from the witness block-signing key `Kw`, which was never intended to carry authorization weight for anything other than block production.

### Citations

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

**File:** actuator/src/main/java/org/tron/core/utils/TransactionUtil.java (L231-244)
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
```

**File:** framework/src/main/java/org/tron/core/Wallet.java (L665-678)
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
          if (!WalletUtil.checkPermissionOperations(permission, contract)) {
            throw new PermissionException("Permission denied!");
          }
        }
```

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1080-1111)
```java
      AccountCapsule account = this.getDeposit().getAccount(address);
      if (account != null) {
        try {
          Permission permission = account.getPermissionById(permissionId);
          if (permission != null) {
            //calculate weight
            long totalWeight = 0L;
            List<byte[]> executedSignList = new ArrayList<>();
            for (byte[] sign : signatures) {
              byte[] recoveredAddr = recoverAddrBySign(sign, hash);

              sign = merge(recoveredAddr, sign);
              if (ByteArray.matrixContains(executedSignList, recoveredAddr)) {
                if (ByteArray.matrixContains(executedSignList, sign)) {
                  continue;
                }
                MUtil.checkCPUTime();
              }
              long weight = TransactionCapsule.getWeight(permission, recoveredAddr);
              if (weight == 0) {
                //incorrect sign
                return Pair.of(true, DATA_FALSE);
              }
              totalWeight += weight;
              executedSignList.add(sign);
              executedSignList.add(recoveredAddr);
            }

            if (totalWeight >= permission.getThreshold()) {
              return Pair.of(true, dataOne());
            }
          }
```

**File:** chainbase/src/main/java/org/tron/core/capsule/AccountCapsule.java (L1280-1298)
```java
  public Permission getPermissionById(int id) {
    if (id == 0) {
      if (this.account.hasOwnerPermission()) {
        return this.account.getOwnerPermission();
      }
      return getDefaultPermission(this.account.getAddress());
    }
    if (id == 1) {
      if (this.account.hasWitnessPermission()) {
        return this.account.getWitnessPermission();
      }
      return null;
    }
    for (Permission permission : this.account.getActivePermissionList()) {
      if (id == permission.getId()) {
        return permission;
      }
    }
    return null;
```

**File:** chainbase/src/main/java/org/tron/core/capsule/BlockCapsule.java (L186-202)
```java
  public boolean validateSignature(DynamicPropertiesStore dynamicPropertiesStore,
      AccountStore accountStore) throws ValidateSignatureException {
    try {
      byte[] sigAddress = SignUtils.signatureToAddress(getRawHash().getBytes(),
          TransactionCapsule.getBase64FromByteString(
              block.getBlockHeader().getWitnessSignature()),
          CommonParameter.getInstance().isECKeyCryptoEngine());
      byte[] witnessAccountAddress = block.getBlockHeader().getRawData().getWitnessAddress()
          .toByteArray();

      if (dynamicPropertiesStore.getAllowMultiSign() != 1) {
        return Arrays.equals(sigAddress, witnessAccountAddress);
      } else {
        byte[] witnessPermissionAddress = accountStore.get(witnessAccountAddress)
            .getWitnessPermissionAddress();
        return Arrays.equals(sigAddress, witnessPermissionAddress);
      }
```

**File:** actuator/src/main/java/org/tron/core/actuator/AccountPermissionUpdateActuator.java (L80-82)
```java
    if (permission.getType() == PermissionType.Witness && permission.getKeysCount() != 1) {
      throw new ContractValidateException("Witness permission's key count should be 1");
    }
```
