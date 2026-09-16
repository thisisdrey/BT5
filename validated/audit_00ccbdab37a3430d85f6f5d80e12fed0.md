### Title
Multisig transaction signature/weight validation is not bound to the `Permission` snapshot in effect when signatures were collected, allowing threshold/weight drift between approval and execution - (File: `chainbase/src/main/java/org/tron/core/capsule/TransactionCapsule.java`)

### Summary
`TransactionCapsule.validateSignature()`/`validatePubSignature()` recover signer addresses from a transaction's signature list and check the accumulated weight against the `Permission` object fetched *live* from the account at validation time (`account.getPermissionById(permissionId)`), not against the `Permission` state that existed when the signers actually produced their signatures. The signed payload (`getTransactionId()` hash) covers only the raw transaction data — it never binds the permission's keys/weights/threshold that were used to justify collecting those particular signatures. This mirrors the OpenClaw class of bug: an "approval" (collected co-signer signatures under a specific weight/threshold configuration) is not bound to the mutable operand (`Permission`, addressed indirectly via `permissionId`) that governs whether that approval is sufficient at execution time.

### Finding Description
`checkWeight()` and `validateSignature()` look up the permission definition from on-chain storage at the moment of verification: [1](#0-0) 

The `permissionId` embedded in the contract only identifies a *slot* on the account (owner id 0, witness id 1, active ids 2–9). The actual `Permission` message (keys, weights, threshold, operations bitmap) stored at that slot is mutable and can be freely replaced via `AccountPermissionUpdateActuator`, which simply overwrites the slot in place: [2](#0-1) [3](#0-2) 

Nothing in the signed transaction (the raw data hashed for `getTransactionId()`, which is what co-signers actually sign in `sign()`/`addSign()`) references the specific `Permission` content, a version number, or a hash of the permission that was in effect when the signatures were solicited: [4](#0-3) 

Consequently, the "approval" (a set of collected co-signer signatures, gathered off-chain or via `GetTransactionSignWeight`/`GetTransactionApprovedList`) and the "execution" (on-chain weight/threshold check in `checkWeight`) are evaluated against two potentially different `Permission` snapshots. Between the time co-signers sign a pending multisig transaction and the time it is broadcast/included in a block, the account owner (who alone controls `AccountPermissionUpdateContract`) can submit a permission update that lowers the threshold or reweights keys for the same `permissionId`, so that a signature set that was insufficient (or intended for a different quorum) becomes sufficient at execution time — without any of the original signers re-consenting to the new quorum rules. This is a direct structural analog of "approval flow did not bind mutable operands across approval and execution": the `permissionId` is the fixed "argv", and the underlying `Permission` object is the mutable, unbound operand.

### Impact Explanation
An account owner (or anyone who otherwise controls the Owner key, e.g., via a partially-compromised key or malicious insider in a custodial/DAO-style multisig) can retroactively change the semantics of a still-pending, partially co-signed transaction by shipping a `AccountPermissionUpdateContract` before the multisig transaction lands, causing that transaction to execute with a weaker consent standard than the signers believed they were authorizing. This can result in unauthorized account operations (transfers, contract triggers, resource delegations) being executed with fewer/weaker real signer approvals than intended, i.e. concrete unauthorized account operation / potential theft of funds under a compromised or negligent multisig setup, which matches the CWE-285/CWE-367 class of the reference advisory.

### Likelihood Explanation
Exploitation requires two ordinary, unprivileged, broadcastable transactions: an `AccountPermissionUpdateContract` from the account (owner-signed) and the pre-collected multisig transaction. No SR/witness/committee privilege, no node compromise, and no P2P manipulation is needed — only normal transaction broadcasting and control of transaction ordering (trivial for the account owner submitting both transactions, or for anyone racing to get their permission update mined ahead of the pending multisig transaction). This is directly reachable through the public wallet API (`broadcastTransaction`, `GetTransactionSignWeight`, `GetTransactionApprovedList`) documented in `framework/src/main/java/org/tron/core/Wallet.java`.

### Recommendation
Bind the approval to an immutable snapshot of the permission that governs it: incorporate a permission version/hash (or full serialized `Permission`) into the data that co-signers actually sign, and re-validate at execution time that the currently stored `Permission` for `permissionId` still matches that bound snapshot (reject if it has drifted, forcing re-collection of signatures under the new permission). At minimum, increment a per-permission-slot version counter on every `AccountPermissionUpdateContract` and require transactions to declare/match the version they were signed against in `checkWeight`/`validateSignature`.

### Proof of Concept
1. Account `A` sets Active permission (`permissionId=2`) with keys `K1` (weight 5) and `K2` (weight 5), threshold 10 (both required).
2. `K1` signs a sensitive transaction `T` (e.g., a large `TransferContract`) — weight 5, insufficient alone under the current permission.
3. Before `T` is broadcast, `A` (Owner key) submits `AccountPermissionUpdateContract` for `permissionId=2`, lowering threshold to 5 (or raising `K1`'s weight to 10), reusing the same slot per `AccountCapsule.updatePermissions()`.
4. `A` broadcasts `T` (signed only by `K1`). `TransactionCapsule.validateSignature()`/`checkWeight()` fetch the *now-updated* `Permission` via `account.getPermissionById(2)` and find weight 5 ≥ new threshold 5, so `T` executes — despite `K2` never approving it and the original quorum requiring both keys.

### Citations

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

**File:** chainbase/src/main/java/org/tron/core/capsule/TransactionCapsule.java (L589-632)
```java
  public void sign(byte[] privateKey) {
    SignInterface cryptoEngine = SignUtils
        .fromPrivate(privateKey, CommonParameter.getInstance().isECKeyCryptoEngine());
    ByteString sig = ByteString.copyFrom(cryptoEngine.Base64toBytes(cryptoEngine
        .signHash(getTransactionId().getBytes())));
    this.transaction = this.transaction.toBuilder().addSignature(sig).build();
  }

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
```

**File:** actuator/src/main/java/org/tron/core/actuator/AccountPermissionUpdateActuator.java (L34-52)
```java
  @Override
  public boolean execute(Object object) throws ContractExeException {
    TransactionResultCapsule result = (TransactionResultCapsule) object;
    if (Objects.isNull(result)) {
      throw new RuntimeException(ActuatorConstant.TX_RESULT_NULL);
    }

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
