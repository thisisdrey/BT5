## Title
`ValidateMultiSign` TVM precompile computes its signature-verification hash without binding to chain id, enabling cross-chain signature replay - ([File: actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java])

### Summary
The `ValidateMultiSign` precompiled contract (address `0x...0a`) is the on-chain primitive TVM contracts use to verify off-chain, permission-scoped multi-signatures (an account-abstraction "permit"-style mechanism). The message hash it verifies signatures against is built purely from `address || permissionId || data`, with no chain identifier mixed in, so a signature produced for one TRON network is valid on any other TRON-based network where the same address/permission exists.

### Finding Description
`ValidateMultiSign.execute` constructs the hash to be verified as: [1](#0-0) 

```
byte[] address = words[0].toTronAddress();
int permissionId = words[1].intValueSafe();
byte[] data = words[2].getData();
byte[] combine = ByteUtil.merge(address, ByteArray.fromInt(permissionId), data);
byte[] hash = Sha256Hash.hash(..., combine);
```

Note that `combine` only contains the target `address`, the `permissionId`, and caller-supplied `data`. There is no chain id, genesis block hash, or any other network-specific value mixed into the pre-image. The recovered signer's weight against the account's `Permission` (fetched via `TransactionCapsule.getWeight`) is then checked purely against locally stored account/permission state: [2](#0-1) 

This precompile is designed to let TVM contracts implement off-chain multisig/permit-style authorization (functionally analogous to `ERC721Permit`/EIP-712 permit flows referenced in the report) by verifying a user's signature over arbitrary `data` against the account's on-chain `Permission`. Because the resulting SHA-256 pre-image never incorporates a chain id, TRON's mainnet, testnets (Nile/Shasta), and any TRON-based fork/private chain that share the same account address and permission configuration will all accept an identical signature. This is the exact bug class from the report: a signature that authorizes an action should be scoped to one chain, but the absence of chain-id hashing lets it be replayed verbatim elsewhere.

### Impact Explanation
Any dApp/contract built on TRON that relies on `ValidateMultiSign` as its authorization/permit mechanism (e.g., gating a withdrawal, order execution, or state-changing call on a valid multisig over `data`) is systemically exposed to cross-chain replay: an attacker who captures a signature broadcast or executed on one TRON-compatible network can resubmit the identical `data`/signature pair to the same precompile call on another network sharing the signer's account/permission, causing the target contract there to treat it as freshly authorized. This can lead to unauthorized execution of a privileged, permission-gated operation (e.g., duplicate approval/withdrawal) that the signer never intended for that chain — i.e., unauthorized account operation, which satisfies the required impact bar.

### Likelihood Explanation
Reachability requires no privileged role: any unprivileged transaction broadcaster can deploy a contract that calls the `ValidateMultiSign` precompile and can then replay a previously observed signature/`data` pair from another chain sharing the same account/permission setup (mainnet vs. testnets, or any TRON fork). The precompile itself performs no chain binding, so exploitation depends only on the existence of a second reachable TRON-protocol network with the same account state — a realistic condition given TRON's mainnet/Nile/Shasta topology and the prevalence of TRON-protocol forks.

### Recommendation
Mix a chain-specific value (e.g., the network's genesis block hash or a configured chain id, similar to how `ALLOW_OPTIMIZED_RETURN_VALUE_OF_CHAIN_ID`/`getChainId()` already expose a chain id in the TVM) into the `combine` pre-image hashed in `ValidateMultiSign.execute`, so the same `address`/`permissionId`/`data` triple produces a distinct verification hash per chain, preventing signature reuse across networks.

### Proof of Concept
1. On Chain A (e.g., TRON Nile testnet), an account `X` with `Permission` `P` (threshold 2, keys `k1`, `k2`) signs `data` via `k1`/`k2` to authorize a contract action, producing `hash = SHA256(address(X) || permissionId(P) || data)` and signatures `sig1, sig2`.
2. A contract on Chain A calls `ValidateMultiSign(address(X), P, data, [sig1, sig2])`, which succeeds and triggers a privileged effect (e.g., releasing funds).
3. The identical `address(X)`/permission configuration exists on Chain B (e.g., TRON mainnet or another TRON-protocol network), because the same account/keys were provisioned there.
4. An attacker (no privileged role required) submits the same `data`, `sig1`, `sig2` to the equivalent contract on Chain B calling `ValidateMultiSign`; since the hash computation in `PrecompiledContracts.ValidateMultiSign.execute` is chain-agnostic, verification succeeds again, re-triggering the privileged effect on Chain B without a fresh authorization from the signer for that chain.

### Citations

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1057-1065)
```java
      DataWord[] words = DataWord.parseArray(rawData);
      byte[] address = words[0].toTronAddress();
      int permissionId = words[1].intValueSafe();
      byte[] data = words[2].getData();

      byte[] combine = ByteUtil.merge(address, ByteArray.fromInt(permissionId), data);
      byte[] hash = Sha256Hash.hash(CommonParameter
          .getInstance().isECKeyCryptoEngine(), combine);

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
