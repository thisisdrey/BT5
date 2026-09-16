This confirms the analog: `TransactionCapsule.checkWeight` at [1](#0-0)  validates signatures by recovering an ECDSA public key/address from each signature and matching it against `Permission.Key` addresses — there is no path for a smart-contract address to ever produce a valid ECDSA signature recoverable this way. Since `AccountPermissionUpdateActuator.checkPermission` only validates format via `DecodeUtil.addressValid` and never rejects contract addresses as `Key` entries, a user can set the `owner` (or all `active`) permission's keys to smart-contract addresses, permanently bricking the account's ability to ever satisfy its signature threshold.

### Title
Permission `Key` addresses in `AccountPermissionUpdateActuator` are not checked to be non-contract, allowing permanent account lockout - (File: `actuator/src/main/java/org/tron/core/actuator/AccountPermissionUpdateActuator.java`)

### Summary
`AccountPermissionUpdateActuator.checkPermission()` validates each `Permission.Key` address only for well-formedness (`DecodeUtil.addressValid`), never checking whether the address belongs to a smart contract account. Because TRON transaction signature verification (`TransactionCapsule.checkWeight`/`validateSignature`) only accepts ECDSA signatures whose recovered address matches a permission key, a contract address (which has no private key and can never produce a valid signature) can never contribute weight to any permission threshold.

### Finding Description
`checkPermission()` in [2](#0-1)  only checks address format validity and duplication, with no check on whether `key.getAddress()` refers to a deployed smart contract (`AccountType.Contract`). This function is used for the `Owner`, `Witness`, and `Active` permissions set via `validate()` at [3](#0-2) .

Signature checking, however, is strictly signature-recovery based: `TransactionCapsule.checkWeight` at [1](#0-0)  recovers an address from each raw ECDSA signature via `SignUtils.signatureToAddress` and matches it to `Key` entries in the permission — it has no notion of contract-based authorization for this path (unlike `PrecompiledContracts.ValidateMultiSign`, which is a separate on-chain precompile usable only from within a TVM execution context, not for top-level transaction authorization). This means if all keys of the `Owner` permission (and, similarly, all `Active`/`Witness` permission keys) are set to smart-contract addresses, no signature can ever satisfy the threshold, because contracts hold no private key capable of producing an ECDSA signature recoverable to that address.

### Impact Explanation
If a user (accidentally, or via a malicious dApp/wallet UI tricking them) submits an `AccountPermissionUpdateContract` transaction that sets the `Owner` permission's key(s) to a contract address, the account permanently loses the ability to ever sign a valid `Owner`-permission transaction again — including any further `AccountPermissionUpdateContract` to fix the mistake. This permanently freezes all TRX, TRC10, and TRC20/other assets, frozen/staked balances, and voting rights controlled by that account, since no further transactions from the `Owner` permission (or an all-contract `Active` permission) can ever be authorized. This is a permanent freezing-of-funds condition reachable from a single signed transaction.

### Likelihood Explanation
This is directly reachable by any account holder broadcasting an ordinary `AccountPermissionUpdateContract` transaction (a standard, low-fee, documented operation for TRON multisig setup) — no privileged access, malicious SR, or special node behavior is required. The only precondition is `AllowMultiSign` being enabled (already the case on mainnet). This closely parallels the analogous KintoWallet report: a user can unintentionally (or be tricked into) supplying a contract address in a security-critical "signer" role, permanently losing access, because the code never disallows it.

### Recommendation
In `AccountPermissionUpdateActuator.checkPermission()`, for every `Key` in `permission.getKeysList()`, look up the account via `AccountStore` and reject the update (throw `ContractValidateException`) if the account's `AccountType` is `Contract` (or if the key address has a deployed contract in `ContractStore`), similarly to how `FreezeBalanceActuator`/`UnfreezeBalanceActuator` already reject delegating resources to contract addresses (`receiverCapsule.getType() == AccountType.Contract`). At minimum, this check should be enforced for the `Owner` permission, and ideally for every `Active`/`Witness` permission key, to prevent a permission from being composed entirely of unusable contract-address keys.

### Proof of Concept
1. Attacker/careless user account `A` holds TRX/frozen balance and has `AllowMultiSign` enabled on-chain (default on mainnet).
2. Deploy or pick any existing smart contract address `C` (no corresponding private key exists).
3. Broadcast an `AccountPermissionUpdateContract` transaction from `A`, signed with `A`'s current valid key, setting the new `Owner` permission's `keys` list to contain only `C` with `weight >= threshold` (validated successfully by `checkPermission`, since `DecodeUtil.addressValid(C)` passes and there's no contract check).
4. `AccountPermissionUpdateActuator.execute()` commits this new `Owner` permission via `account.updatePermissions(...)` at [4](#0-3) .
5. From now on, any transaction requiring the `Owner` permission (including further `AccountPermissionUpdateContract` transactions to undo the mistake) must be signed such that `TransactionCapsule.checkWeight` recovers address `C` from a valid ECDSA signature — which is impossible since `C` is a contract with no private key.
6. Account `A`'s funds and control are now permanently frozen.

### Citations

**File:** chainbase/src/main/java/org/tron/core/capsule/TransactionCapsule.java (L233-256)
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
```

**File:** actuator/src/main/java/org/tron/core/actuator/AccountPermissionUpdateActuator.java (L47-52)
```java
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

**File:** actuator/src/main/java/org/tron/core/actuator/AccountPermissionUpdateActuator.java (L208-227)
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
```
