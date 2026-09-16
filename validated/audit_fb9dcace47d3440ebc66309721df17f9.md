## Analog Vulnerability Found

### Title
Custom signature hash in `ValidateMultiSign` TVM precompile lacks contract/domain binding, enabling cross-contract signature replay - ([File: actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java])

### Summary
The `ValidateMultiSign` precompiled contract (address `0x...0a`, callable by any TVM smart contract) implements its own homemade signature-verification scheme instead of a standards-based, domain-separated one (e.g. EIP-712 style). It hashes only `owner_address || permissionId || data` and recovers/validates signer weight against that hash, with no binding to the calling/verifying contract's address, chain id, or any nonce/expiry.

### Finding Description
`ValidateMultiSign.execute` builds the message to be verified as: [1](#0-0) 

then recovers each signer's address from the raw ECDSA signature and accumulates permission weight from the account's `Permission`: [2](#0-1) 

`recoverAddrBySign` is the raw ECDSA recovery helper with no context checks: [3](#0-2) 

Unlike EIP-712, which mandates a domain separator binding a signature to `(contract address, chainId, ...)` plus an application-defined nonce/expiry, this precompile bakes in only `owner_address` and `permissionId` as "domain" context and leaves the remaining message content (`data`) entirely to the calling smart contract. Because the precompile is globally reachable by any deployed contract, and multiple unrelated dApps can legitimately call `ValidateMultiSign` for the same account/`permissionId`, any off-chain signature a user produces for one consuming contract's `data` payload can be replayed against any other contract that happens to construct (or can be induced to construct) the same `(owner_address, permissionId, data)` triple — the precompile itself provides no protection against this because it never binds the signature to "who is asking" (the calling contract) or to a nonce/expiration. This mirrors exactly the bug class in the report: a self-rolled sign/verify scheme that omits domain separation, nonce, and proper context binding, unlike battle-tested EIP-712/OpenZeppelin ECDSA usage.

### Impact Explanation
Any Tron account holder using multisig permissions and any dApp built on top of `ValidateMultiSign` inherits this weakness: an authorization signature crafted/obtained for one contract's business logic can be reused/replayed to satisfy the permission-weight check inside a different contract if the `data` payload collides or is attacker-influenced, since the precompile has no per-consumer domain separator. This can lead to unauthorized execution of a permissioned action (satisfying `weight >= threshold`) in a context the signer never intended to authorize — i.e., unauthorized account operation gated behind multisig, potentially resulting in theft of funds if a dApp relies solely on this precompile output to gate a transfer/withdrawal.

### Likelihood Explanation
The precompile is directly reachable by any unprivileged contract deployer/caller via a single contract call (energy-metered, no special privilege required). Exploitability depends on a downstream dApp's specific `data` construction, but the root cause — a hand-rolled hash construction without contract/chainId/nonce binding — is entirely within java-tron's own precompile implementation and is unconditionally present for every consumer of `ValidateMultiSign`, making the underlying weakness systemic rather than contract-specific.

### Recommendation
Extend the hashed message in `ValidateMultiSign` to include a domain separator analogous to EIP-712: bind the signature to the calling contract's address (`msg.sender`/verifying contract), the chain id, and require the consuming contract to supply an explicit nonce/expiry inside `data`, or document/enforce this requirement so signatures cannot be replayed across unrelated consuming contracts. Consider adopting a standard EIP-712-style domain-separated hashing scheme rather than the current `Sha256(address || permissionId || data)` construction.

### Proof of Concept
1. Deploy Contract A and Contract B, both calling the `ValidateMultiSign` precompile for the same user account `addr` and `permissionId`.
2. User signs off-chain over `data = X` intending to authorize a specific action in Contract A (e.g., "approve transfer of 100 TRX").
3. Contract B, designed to accept the same encoding for a different action (or an attacker crafts Contract B to reuse the identical `data` field, e.g., generic "approve amount" payload shared across multiple dApps), calls `ValidateMultiSign(addr, permissionId, X, [signature])`.
4. Because the hash is `Sha256(addr || permissionId || X)` — identical in both contracts — the same signature passes weight validation in Contract B, authorizing an action the signer never approved for B. [4](#0-3)

### Citations

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L371-388)
```java
  private static byte[] recoverAddrBySign(byte[] sign, byte[] hash) {
    byte[] out = null;
    if (ArrayUtils.isEmpty(sign) || sign.length < 65) {
      return new byte[0];
    }
    try {
      Rsv rsv = Rsv.fromSignature(sign);
      SignatureInterface signature = SignUtils.fromComponents(rsv.getR(), rsv.getS(), rsv.getV(),
          CommonParameter.getInstance().isECKeyCryptoEngine());
      if (signature.validateComponents()) {
        out = SignUtils.signatureToAddress(hash, signature,
            CommonParameter.getInstance().isECKeyCryptoEngine());
      }
    } catch (Throwable any) {
      logger.info("ECRecover error", any.getMessage());
    }
    return out;
  }
```

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1057-1110)
```java
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
