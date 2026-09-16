### Title
Cross-chain signature replay in the `ValidateMultiSign` TVM precompile due to missing chain-binding in the signed hash - (File: `actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java`)

### Summary
The `ValidateMultiSign` precompiled contract (TVM address `0x0a`) lets any deployed smart contract verify that an off-chain signature was produced by enough weight of keys under a TRON account's permission. The hash that is actually signed is computed internally by java-tron from only `address || permissionId || data` — it contains no chain identifier, genesis-block hash, or any other domain separator. Because TRON account addresses are deterministic from the public key alone (independent of which TRON-compatible network the node belongs to), the exact same address/permission/signature triple is valid on every network that shares that account (mainnet, Nile/Shasta testnets, or any private/forked java-tron chain). This mirrors the root cause in the original report: a signature-authorization primitive that omits chain binding, while a "correct" domain-separated verification path exists elsewhere in the codebase (ordinary transaction signing, which is implicitly bound to a specific chain via `ref_block_hash`/`expiration` matched against real block state).

### Finding Description
`ValidateMultiSign.execute` builds the message to verify as follows: [1](#0-0) 

```
DataWord[] words = DataWord.parseArray(rawData);
byte[] address = words[0].toTronAddress();
int permissionId = words[1].intValueSafe();
byte[] data = words[2].getData();

byte[] combine = ByteUtil.merge(address, ByteArray.fromInt(permissionId), data);
byte[] hash = Sha256Hash.hash(CommonParameter.getInstance().isECKeyCryptoEngine(), combine);
```

It then recovers the signer for each supplied signature against that hash and checks their permission weight against the account's threshold: [2](#0-1) 

No element of this hash construction (`address`, `permissionId`, `data`) is chain-specific. Unlike an ordinary TRON transaction — whose signed payload embeds `ref_block_hash`/`expiration`, which are only valid against the concrete block history of one specific chain, providing implicit domain separation (see `TransactionCapsule.validateSignature`/`checkWeight`) — this precompile's hash is fully chain-agnostic: [3](#0-2) 

Because TRON addresses are derived purely from the public key (`ECKey.computeAddress`), the same address and, typically, the same active-permission key set exist identically on any java-tron-based network the user's key also controls (mainnet, testnets, private forks/sidechains). A contract author who uses `ValidateMultiSign` as an off-chain authorization/claim mechanism — e.g., "user signs `data` off-chain, later redeems it on-chain by calling `validatemultisign(address, permissionId, data, sigs)`", which is exactly the pattern used by Phi's `signatureClaim` — has no way to prevent the same signature from being replayed verbatim on a different network where the identical address/permission exists, because java-tron itself never mixed in any chain identifier when constructing the hash to be signed.

### Impact Explanation
Any dApp built on top of `ValidateMultiSign` (address `0x0a`) for signature-gated actions — reward/airdrop claims, gasless meta-transactions, off-chain approvals redeemed on-chain, etc. — inherits a cross-chain replay vulnerability it cannot itself fix at the calling-contract level, because the vulnerable hash construction happens inside the precompile, in java-tron's own code, not in caller-supplied data. An attacker who obtains a validly-authorized signature (e.g., by triggering a low-value/no-cost redemption on one network) can replay it on another network sharing the same account, causing unauthorized execution of the permissioned action there — e.g. draining/claiming assets, approving transfers, or executing privileged calls that were never actually authorized for that chain. This is a direct, unbounded-value theft/unauthorized-operation vector reachable by any unprivileged contract deployer.

### Likelihood Explanation
Reaching this path requires only deploying a normal smart contract that calls the `validatemultisign` precompile (address `0x0a`), which is a supported, documented TVM primitive activated by `VMConfig.allowTvmSolidity059()` and enabled on all networks. No special privilege, witness/SR role, or peer/network position is needed — any transaction broadcaster or contract deployer can build and exploit such a flow, and the pattern (sign off-chain, redeem on-chain via this precompile) is exactly the kind of "claim" architecture the original report's bug class targets.

### Recommendation
Mix a chain-specific domain separator into the hash computed by `ValidateMultiSign` (and ideally document the same requirement for `BatchValidateSign`, whose caller supplies the raw hash directly), for example incorporating the node's configured chain id / genesis block hash (`CommonParameter`/`Args` chain id equivalent) alongside `address`, `permissionId`, and `data` before hashing: [4](#0-3) 

so that signatures produced for one network cannot be verified as valid on any other java-tron-based network sharing the same account/permission state.

### Proof of Concept
1. Deploy a contract `Claim` on Network A (e.g. a testnet) that calls `validatemultisign(msg.sender, permissionId, data, sigs)` to gate a reward/withdrawal.
2. User with address `X` (private key `K`) signs `data` off-chain per the `combine = address || permissionId || data` scheme used by `ValidateMultiSign` and redeems the reward on Network A — the call succeeds because `TransactionCapsule.getWeight`/permission threshold is satisfied.
3. Deploy the identical `Claim` contract at the same logic on Network B (mainnet, or any other java-tron chain) where address `X` (same key `K`) also has an active permission set (a very common real-world setup, since the same key controls address `X` identically on every TRON-family chain).
4. Replay the exact same `sigs`/`data` obtained in step 2 against Network B's `Claim` contract — `ValidateMultiSign` recomputes the identical hash (`address || permissionId || data`, no chain id) and recovers the same signer, satisfying the permission threshold again, allowing unauthorized re-execution of the gated action on Network B despite the signature never having been issued for that chain.

### Citations

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1057-1064)
```java
      DataWord[] words = DataWord.parseArray(rawData);
      byte[] address = words[0].toTronAddress();
      int permissionId = words[1].intValueSafe();
      byte[] data = words[2].getData();

      byte[] combine = ByteUtil.merge(address, ByteArray.fromInt(permissionId), data);
      byte[] hash = Sha256Hash.hash(CommonParameter
          .getInstance().isECKeyCryptoEngine(), combine);
```

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1080-1110)
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
