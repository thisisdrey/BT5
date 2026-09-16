## Analysis

The Sherlock report's bug class is: **a hash used to authorize/validate an action is computed without any chain-specific salt (chainId), so the same hash — and therefore the same off-chain signature over it — validates identically on multiple chains**, letting an action approved on one chain be replayed as an approval on another.

The closest reachable analog in java-tron is the `ValidateMultiSign` precompiled contract, exposed at TVM precompile address `0x0000...16` and callable by any smart contract from any signed transaction.

### Root cause

`ValidateMultiSign.execute()` builds the hash that signatures are checked against purely from the TRON account address, permission id, and caller-supplied `data` — with no chain identifier, genesis hash, or any other network-binding value: [1](#0-0) 

```java
byte[] combine = ByteUtil.merge(address, ByteArray.fromInt(permissionId), data);
byte[] hash = Sha256Hash.hash(CommonParameter
    .getInstance().isECKeyCryptoEngine(), combine);
```

The recovered signer addresses are then weighed against the target account's on-chain `Permission` to decide pass/fail: [2](#0-1) 

Because `address`, `permissionId`, and `data` are the only inputs to the hash, an identical account (same address, same permission configuration) that exists on more than one TRON‑protocol network — mainnet, a public testnet (Nile/Shasta), or any private/consortium chain forked from the same genesis/account snapshot — produces **the exact same hash and therefore accepts the exact same signature** on every one of those chains. There is no equivalent of Ethereum's EIP‑155 chainId mixed into the digest, unlike the reference report's `getTransactionHash` in the audited Safe zodiac module.

This is a genuine analog of the reported issue: signature intended for authorization on chain A can be replayed against the identical precompile call on chain B to satisfy the same multisig threshold check, causing a smart contract relying on `ValidateMultiSign` for access control to treat a cross-chain-replayed signature as valid, enabling unauthorized on-chain actions (e.g., unauthorized withdrawal/approval logic gated by this precompile).

By contrast, ordinary top-level transaction signing/validation in `TransactionCapsule` (`getTransactionId`, `checkWeight`, `validatePubSignature`) is indirectly bound to chain state via `ref_block_hash`/expiration and account existence checks, so it doesn't share this exact weakness in the same direct way: [3](#0-2) 

### Title
Cross-chain signature replay in `ValidateMultiSign` precompile due to missing chain identifier in signed hash - (File: actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java)

### Summary
The `ValidateMultiSign` TVM precompile computes the hash that off-chain signatures must cover from only `address`, `permissionId`, and caller-supplied `data`, omitting any chain-specific value (chainId/genesis hash). Any TRON-protocol chain sharing the same account/permission state (mainnet, public testnets, private forks/sidechains) will accept an identical signature for the identical hash.

### Finding Description
`execute()` builds `combine = address || permissionId || data` and hashes it with `Sha256Hash.hash`, then recovers signer addresses from that hash and sums their permission weights against the account's threshold: [4](#0-3) 
No chain-binding value is included, so the digest — and any signature produced over it — is chain-agnostic.

### Impact Explanation
A smart contract using this precompile as its access-control/multisig-verification primitive (a documented and commonly used TRON pattern for on-chain multisig verification) can be tricked into accepting a signature that was produced/authorized for a different chain sharing the same account and permission layout, resulting in unauthorized execution of privileged contract logic gated by this check (e.g., approving a withdrawal, config change, or asset transfer without the intended chain-specific authorization).

### Likelihood Explanation
Exploitation requires the same account address and identical `Permission` configuration to exist on more than one TRON-based network (realistic for testnets/mainnet snapshot forks, private chains, or sidechains bootstrapped from the same account state) and for an attacker to obtain a signature valid on one such chain. Given multiple TRON-derived networks and sidechains exist with shared account/key material, this is plausible though environment-dependent, consistent with Medium severity.

### Recommendation
Mix a chain-specific constant (e.g., `chainId`/genesis block hash from `DynamicPropertiesStore`) into the `combine` buffer before hashing in `ValidateMultiSign.execute()`, so the same address/permission/data cannot produce an identical verifiable hash across different chains.

### Proof of Concept
1. Deploy identical contracts and account/permission structures (same address, same `Permission`) on two different TRON-protocol networks (e.g., a private testnet forked from mainnet's account snapshot).
2. Have the permission's key holders sign `hash = Sha256Hash.hash(address || permissionId || data)` intending to authorize an action on network A.
3. Submit the same signature/data to a contract on network B calling `ValidateMultiSign` — the precompile computes the identical hash and accepts the signature, authorizing the action on network B without independent consent.

### Citations

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
