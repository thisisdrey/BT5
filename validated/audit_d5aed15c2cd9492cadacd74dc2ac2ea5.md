## Analog Found

### Title
Missing Domain Separation in TVM `ValidateMultiSign` Precompile Enables Cross-Contract Signature Replay - (File: `actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java`)

### Summary
The `ValidateMultiSign` precompiled contract, used by TVM smart contracts to verify off-chain signatures against an on-chain account `Permission`, hashes only `address || permissionId || data` with no binding to the calling contract's address, its bytecode/purpose, or the chain ID. This mirrors the LGO `DOMAIN_SEPARATOR`-omission bug class: a signature a user produces to authorize one contract's action can be replayed verbatim by any other (including a malicious) contract that reconstructs the same `(address, permissionId, data)` triple.

### Finding Description
The precompile builds the message hash as: [1](#0-0) 
and later resolves the account's permission and tallies recovered-signer weight purely from that hash, with no reference anywhere to `msg.sender`/contract address or chain identifier: [2](#0-1) 

Contrast this with full on-chain transaction signing, where `TransactionCapsule.validateSignature`/`checkWeight` hash the entire raw transaction, which embeds a TaPoS reference (`ref_block_hash`/`ref_block_bytes`) tying the signature to a specific block on a specific chain: [3](#0-2) [4](#0-3) 

That TaPoS-style domain separation is absent from `ValidateMultiSign`: `data` is fully attacker/contract-controlled call input, and nothing forces it to encode the calling contract's own address or a network/application identifier. Any two TVM contracts (deployed by different, mutually-untrusting authors, on the same or forked chain) that happen to construct an identical `(address, permissionId, data)` tuple to mean different things will accept the same off-chain signature as valid authorization — exactly the "signed message reusable across unrelated applications" pattern described in the LGO report for the missing EIP-712 `DOMAIN_SEPARATOR`.

### Impact Explanation
A user who signs a message off-chain to authorize an action in one contract (e.g., a benign dApp using this generic multisig verification pattern) can have that same signature replayed by an unrelated, attacker-deployed contract that also calls `ValidateMultiSign` with the matching `address`/`permissionId`/`data`, causing the attacker's contract to treat the user as having approved an action they never intended for that contract. Since `ValidateMultiSign` is the on-chain trust anchor many TVM contracts use to gate privileged operations (transfers, approvals, withdrawals) behind a user's Active/Owner permission signature, this can lead to unauthorized account operations or theft of funds, satisfying the "unauthorized account operation / theft of funds" impact bar.

### Likelihood Explanation
Exploitation requires no privileged access: any unprivileged contract deployer can deploy a contract that calls this precompile with attacker-chosen `data`, and the only precondition is that a legitimate signature exists (or can be induced, e.g., via a look-alike dApp/game/airdrop-claim flow) whose `(address, permissionId, data)` matches what the attacker's contract expects. Because `data` is a raw, unconstrained byte string with no mandated encoding of the target contract or purpose, protocol designers naturally reuse simple/short encodings (e.g., `(amount, nonce)`), increasing the chance of collisions across independently-developed contracts — directly analogous to the report's Alice/Bob replay scenario.

### Recommendation
Bind the precompile's hash to a domain that uniquely scopes the signature to the invoking contract and chain, e.g. `sha256(address || permissionId || callingContractAddress || chainId || data)`, or require callers to prefix `data` with a caller-specific, protocol-enforced domain tag before hashing (analogous to EIP-712's `DOMAIN_SEPARATOR`). This should be introduced as a new, opt-in TVM precompile behavior (or via `VMConfig`-gated hard fork) so existing integrators can migrate without breaking previously-issued valid signatures outright.

### Proof of Concept
1. Contract A (a legitimate dApp) asks user Alice to sign `data = abi.encode(amount=100, purpose="withdraw")`, and internally calls `ValidateMultiSign(alice, 0, data, [sig])` to authorize a 100-unit withdrawal.
2. Attacker deploys Contract B, which independently decided to use the identical encoding `data = abi.encode(amount, purpose)` for a completely different, higher-value action (e.g., "transfer ownership" or "drain vault" keyed by the same tuple shape), and also calls `ValidateMultiSign(alice, 0, data, [sig])`.
3. Attacker submits Alice's previously-published/leaked signature (broadcast in Contract A's transaction, hence publicly visible on-chain) to Contract B.
4. `ValidateMultiSign` recomputes the identical `sha256(address || permissionId || data)` shown in `PrecompiledContracts.java:1062-1064`, recovers Alice's key from `sig`, finds sufficient permission weight via `TransactionCapsule.getWeight`, and returns success — Contract B treats Alice as having authorized its unrelated, attacker-defined action. [5](#0-4)

### Citations

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1052-1110)
```java
    public Pair<Boolean, byte[]> execute(byte[] rawData) {
      if (VMConfig.allowTvmOsaka()
          && !isValidAbiEncoding(rawData, ABI_HEADER_WORDS, ABI_ITEM_WORDS)) {
        return Pair.of(false, EMPTY_BYTE_ARRAY);
      }
      DataWord[] words = DataWord.parseArray(rawData);
      byte[] address = words[0].toTronAddress();
      int permissionId = words[1].intValueSafe();
      byte[] data = words[2].getData();

      byte[] combine = ByteUtil.merge(address, ByteArray.fromInt(permissionId), data);
      byte[] hash = Sha256Hash.hash(CommonParameter
          .getInstance().isECKeyCryptoEngine(), combine);

      if (VMConfig.allowTvmSelfdestructRestriction()) {
        int sigArraySize = words[words[3].intValueSafe() / WORD_SIZE].intValueSafe();
        if (sigArraySize > MAX_SIZE) {
          return Pair.of(true, DATA_FALSE);
        }
      }
      byte[][] signatures = VMConfig.allowTvmSelfdestructRestriction() ?
          extractSigArray(words, words[3].intValueSafe() / WORD_SIZE, rawData) :
          extractBytesArray(words, words[3].intValueSafe() / WORD_SIZE, rawData);

      if (signatures.length == 0 || signatures.length > MAX_SIZE) {
        return Pair.of(true, DATA_FALSE);
      }

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
```

**File:** framework/src/main/java/org/tron/core/db/Manager.java (L798-823)
```java
  void validateTapos(TransactionCapsule transactionCapsule) throws TaposException {
    byte[] refBlockHash = transactionCapsule.getInstance()
        .getRawData().getRefBlockHash().toByteArray();
    byte[] refBlockNumBytes = transactionCapsule.getInstance()
        .getRawData().getRefBlockBytes().toByteArray();
    try {
      byte[] blockHash = chainBaseManager.getRecentBlockStore().get(refBlockNumBytes).getData();
      if (!Arrays.equals(blockHash, refBlockHash)) {
        String str = String.format(
            "Tapos failed, different block hash, %s, %s , recent block %s, "
                + "solid block %s head block %s",
            ByteArray.toLong(refBlockNumBytes), Hex.toHexString(refBlockHash),
            Hex.toHexString(blockHash),
            chainBaseManager.getSolidBlockId().getString(),
            chainBaseManager.getHeadBlockId().getString());
        throw new TaposException(str);
      }
    } catch (ItemNotFoundException e) {
      String str = String
          .format("Tapos failed, block not found, ref block %s, %s , solid block %s head block %s",
              ByteArray.toLong(refBlockNumBytes), Hex.toHexString(refBlockHash),
              chainBaseManager.getSolidBlockId().getString(),
              chainBaseManager.getHeadBlockId().getString());
      throw new TaposException(str);
    }
  }
```

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
