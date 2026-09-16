### Title
Permission `operations` scope restriction can be bypassed via the TVM `ValidateMultiSign`/`BatchValidateSign` precompiles - (File: actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java)

### Summary
TRON's multisig model lets an account owner restrict what an `Active` permission's keys may authorize by encoding an `operations` bitmap that is checked against the TRON `ContractType` of whatever they try to sign [1](#0-0) . This restriction ("security policy") is enforced consistently on the normal transaction-signing/broadcast path via `checkPermissionOperations`/`checkPermission` [2](#0-1)  and in the corresponding query APIs (`getTransactionSignWeight`, `getTransactionApprovedList`) [3](#0-2) [4](#0-3) . However, the `ValidateMultiSign` precompiled contract that TVM smart contracts can call directly looks up an account's permission by `permissionId` and only checks signature weight against the permission's threshold — it never calls `checkPermissionOperations` and never restricts `permission.getType()` to `Active` [5](#0-4) .

### Finding Description
The `operations` field on an `Active` `Permission` is the on-chain analog of the reported "allowed-gadgets"/policy-restriction mechanism: an account owner uses `AccountPermissionUpdateContract` to grant a co-signer a key that is only valid for specific `ContractType`s (e.g., transfers only, not `AccountPermissionUpdateContract` or `WitnessCreateContract`), enforced bit-by-bit against `DynamicPropertiesStore.getAvailableContractType()` [6](#0-5) .

Every code path that consumes this permission for an actual TRON transaction re-validates the `operations` bitmap before accepting the signature as authorization:
- `TransactionCapsule.checkPermission()` / `validateSignature()` (used by `Manager.pushTransaction` and block application) [7](#0-6) 
- `TransactionCapsule.addSign()` (wallet-side co-signing) [8](#0-7) 
- `TransactionUtil.getTransactionSignWeight()` and `Wallet.getTransactionApprovedList()` (gRPC/HTTP query APIs) [3](#0-2) [4](#0-3) 

By contrast, the `ValidateMultiSign` precompile — reachable by any contract call (`TriggerSmartContract`, i.e. any unprivileged caller) through `PrecompiledContracts` — fetches `account.getPermissionById(permissionId)` for an arbitrary attacker-supplied `permissionId` and address, then only computes `TransactionCapsule.getWeight(permission, recoveredAddr)` against `permission.getThreshold()` [5](#0-4) . It does not call `checkPermissionOperations`, nor does it restrict which `PermissionType` (Owner/Witness/Active) is being consulted. This means the `operations` scoping an account owner deliberately imposed on a co-signing key (to prevent that key from being usable for anything beyond a narrow whitelist of `ContractType`s) has zero effect once that key's signature is checked through this TVM primitive instead of through native transaction signing.

### Impact Explanation
Any TVM contract (deployed by anyone, permissionlessly) that uses `ValidateMultiSign`/`BatchValidateSign` as an authorization gate for its own logic (a very common multisig-wallet-in-Solidity pattern recommended by TRON documentation) will treat a signature from a scope-restricted `Active` key as fully authorizing whatever the contract does — money transfers, ownership changes, etc. — even though the account owner explicitly restricted that key via `operations` to a narrow, unrelated set of native `ContractType`s. This is a direct violation of the account owner's configured authorization policy and can lead to unauthorized use of a restricted key to sanction actions (e.g., moving TRC-20/TRC-10 balances held by the smart-contract wallet) the key holder was never supposed to be trusted with — a concrete unauthorized-account-operation / fund-theft class impact, consistent with CWE-285 (Improper Authorization).

### Likelihood Explanation
The precompile is reachable from any address via a normal `TriggerSmartContract` transaction with no special privilege required (matches the "malicious client with normal API access" threat model of the advisory). Exploitation requires only: (1) an account owner using `AccountPermissionUpdateContract` to grant a restricted Active key, and (2) a smart contract (attacker-controlled, or any third-party contract) that treats `ValidateMultiSign` success as authorization. Given TRON's documented pattern of using this precompile for on-chain multisig wallets, this is a realistic and directly reachable condition, not purely theoretical.

### Recommendation
In `PrecompiledContracts.ValidateMultiSign.execute()` and the batch variant, after resolving `permission = account.getPermissionById(permissionId)`, additionally invoke the same `operations`/`PermissionType` check used elsewhere (`WalletUtil.checkPermissionOperations` or an equivalent contract-type-aware gate), or explicitly document/restrict the precompile to only accept `Active` permissions whose `operations` bitmap is unrestricted, so the account owner's configured signing-scope policy cannot be silently bypassed through TVM.

### Proof of Concept
1. Account `A` runs `AccountPermissionUpdateContract` creating an `Active` permission (id=2) for key `K`, with `operations` bitmap allowing only `TransferContract` (as validated/tested in `AccountPermissionUpdateActuatorTest.activePermissionInvalidOperationBit`) [9](#0-8) .
2. Any normal transaction signed with `K` for a disallowed `ContractType` (e.g. `AccountPermissionUpdateContract`) is correctly rejected by `checkPermissionOperations` in `TransactionCapsule.checkPermission` [2](#0-1) .
3. An attacker deploys (or uses an existing) TVM contract that calls the `ValidateMultiSign` precompile with `(A, permissionId=2, arbitraryHash, [sig by K over arbitraryHash])`. Per `PrecompiledContracts.ValidateMultiSign.execute()`, this returns `true`/weight-satisfied purely from `getWeight(permission, recoveredAddr) >= permission.getThreshold()`, with no `operations` check performed [5](#0-4) .
4. The calling contract, believing `K`'s authorization is valid for the action it gates (which is not a `TransferContract`), proceeds — bypassing the operations-scope restriction that `A` set up specifically to prevent `K` from being used for exactly this kind of action.

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

**File:** chainbase/src/main/java/org/tron/core/capsule/TransactionCapsule.java (L468-491)
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
```

**File:** chainbase/src/main/java/org/tron/core/capsule/TransactionCapsule.java (L597-622)
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

**File:** framework/src/test/java/org/tron/core/actuator/AccountPermissionUpdateActuatorTest.java (L867-890)
```java
  @Test
  public void activePermissionInvalidOperationBit() {
    ByteString address = ByteString.copyFrom(ByteArray.fromHexString(OWNER_ADDRESS));

    Permission ownerPermission = AccountCapsule.createDefaultOwnerPermission(address);
    Permission activePermission = Permission.newBuilder().setType(PermissionType.Active)
        .setPermissionName("active")
        .setThreshold(1)
        .setOperations(ByteString
            .copyFrom(ByteArray
                .fromHexString("8000000000000000000000000000000000000000000000000000000000000000")))
        .setParentId(0).addKeys(VALID_KEY).build();

    List<Permission> activeList = new ArrayList<>();
    activeList.add(activePermission);

    AccountPermissionUpdateActuator actuator = new AccountPermissionUpdateActuator();
    actuator.setChainBaseManager(dbManager.getChainBaseManager())
        .setAny(getContract(address, ownerPermission, null, activeList));
    TransactionResultCapsule ret = new TransactionResultCapsule();

    processAndCheckInvalid(actuator, ret, "7 isn't a validate ContractType",
        "7 isn't a validate ContractType");
  }
```
