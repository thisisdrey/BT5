Confirmed: the `ValidateMultiSign` precompile hash construction has no chain ID or verifying-contract address, and no other TVM opcode in `PrecompiledContracts.java` adds domain separation to this signature scheme.

### Title
Missing Domain Separation in `ValidateMultiSign` Precompile Allows Cross-Contract/Cross-Chain Signature Replay - (File: `actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java`)

### Summary
The `validatemultisign(address,uint256,bytes32,bytes[])` TVM precompile recovers signer addresses from a hash built solely from `(address, permissionId, data)`, with no chain ID or verifying-contract binding. Any smart contract deployed by any unprivileged user can invoke this precompile with the exact same tuple that was signed for a different application, and reuse a previously observed valid multisig signature to satisfy its own permission-weight check.

### Finding Description
`ValidateMultiSign.execute()` builds the message to be verified as:
```
combine = address || permissionId || data
hash = Sha256Hash.hash(combine)
``` [1](#0-0) 

It then recovers the signer for each supplied signature over this `hash` and sums the weights of matched keys in the account's `Permission`, returning success once the threshold is met: [2](#0-1) 

This is structurally the same defect described in the SGX `addInstances()` report: a signed hash is missing fields that bind it to a specific context of use. In the SGX case, `newInstance` was excluded from `signedHash`, letting the caller redirect a valid attestation signature to an attacker-controlled instance address. Here, the hash excludes `address(this)` (the calling/verifying contract) and `block.chainid`, so a signature produced to authorize one action in one smart contract (or one chain) can be replayed verbatim by *any other unrelated contract* on the same or a different Tron-compatible chain, as long as it happens to submit the same `(address, permissionId, data)` triple to the precompile. Since `data` is fully attacker/caller-controlled and only needs to match what a legitimate signer once signed, and since this signature/tuple pair becomes public the moment it is submitted on-chain (it must be passed as calldata to trigger the precompile), any contract deployer can capture it and replay it in their own contract's `validatemultisign` check to spoof multisig authorization intended for a different application.

### Impact Explanation
`ValidateMultiSign` is exposed to arbitrary smart contracts as a building block for on-chain multisig gating of sensitive actions (e.g., authorizing fund releases, permission-gated operations) using off-chain-collected signatures. Because the hash carries no domain separator, a signature that a group of key-holders produced to authorize a specific action in one dApp can be captured from that dApp's public transaction and replayed by a different, unrelated (and possibly malicious) contract to pass its own `totalWeight >= permission.getThreshold()` check for the same account/permission, as long as the malicious contract can arrange for its `data` argument to match the previously signed value. This can lead to unauthorized account operations being approved (e.g., withdrawal/authorization logic in a victim's dApp being satisfied by a signature never intended for it), i.e., theft or unauthorized state changes gated by that permission.

### Likelihood Explanation
Any user can deploy a TVM smart contract and call `ValidateMultiSign` (reachable via ordinary `TriggerSmartContract` transactions), and any signature+data submitted through this precompile is publicly visible on-chain once used. No SR/witness/committee privilege or off-chain access is required — only observation of a prior on-chain transaction and deployment of a contract that reuses the same `(address, permissionId, data)` tuple.

### Recommendation
Include a domain separator in the hash computed by `ValidateMultiSign`, binding it at minimum to `block.chainid` and ideally to the calling contract address (`address(this)` from the caller's perspective, i.e., the precompile's caller context) so a signature cannot be replayed across different verifying contracts or different chains. This mirrors the report's recommendation to add EIP-712-style domain separation (`address(this)`, `block.chainid`) to `signedHash` in the SGX case.

### Proof of Concept
1. DApp A deploys a contract that calls `validatemultisign(accountA, permissionIdA, dataA, sigs)` to gate a withdrawal, where `sigs` are collected off-chain from `accountA`'s active-permission keyholders specifically for DApp A's intended action encoded implicitly by `dataA`.
2. This transaction (and thus `dataA` and `sigs`) becomes public on-chain.
3. An attacker deploys DApp B, unrelated to DApp A, whose logic happens to call (or can be crafted to call) `validatemultisign(accountA, permissionIdA, dataA, sigs)` with the identical captured tuple.
4. `ValidateMultiSign.execute()` recomputes the identical `hash` (no domain separation), recovers the same signer addresses, and returns success (`DataWord.ONE`) even though the signers never intended to authorize anything in DApp B — [3](#0-2) .

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
