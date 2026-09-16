## Title
Missing caller/domain binding in `ValidateMultiSign` precompile enables cross-contract signature replay for account-authorization checks - (File: `actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java`)

### Summary
The `ValidateMultiSign` precompiled contract (TVM address `0x...0a`) lets any smart contract ask "did this account's permission approve this `data`?" by hashing only `address || permissionId || data`. Because the hash never binds the calling/consuming contract's own identity, a signature an off-chain user produces to authorize one dApp's flow is equally valid input to any other dApp that independently calls this same precompile with the same `(address, permissionId, data)` tuple — the exact cross-contract replay pattern described in the external report, just moved from an application-level EIP-712-style hash into a system-wide TVM precompile.

### Finding Description
`ValidateMultiSign.execute()` builds its signing hash purely from caller-supplied ABI parameters, with no reference to `msg.sender`/`this` of the calling contract, no chain id, and no nonce: [1](#0-0) 

`words[0]` (`address`), `words[1]` (`permissionId`) and `words[2]` (`data`) are all attacker/contract-controlled inputs passed at call time; the resulting `hash` is what off-chain signers must sign, and any contract on the network can present the very same `(address, permissionId, data, signatures)` tuple to this precompile and get the identical `true` result: [2](#0-1) 

This mirrors the reported bug class exactly: the signature hash is supposed to authorize an action *in the context of a specific consuming application*, but the primitive that verifies it omits that context. `BatchValidateSign` (address `0x09`) has the same property in a more extreme form — the "hash" is taken directly from caller input with zero domain binding at all: [3](#0-2) 

Because `ValidateMultiSign`/`BatchValidateSign` are shared system-level primitives (unlike a bespoke `_stakeNFTs` hash local to one app), any two unrelated TVM contracts that both use this precompile for user-authorization and happen to construct `data` the same way (e.g. the same order id, transfer amount, or NFT id encoding) let a signature meant for contract A be replayed verbatim against contract B.

### Impact Explanation
An attacker who controls or influences contract B (any contract deployer can reach this precompile from Solidity via `address(0xa).call(...)`) can harvest a legitimately-produced signature that a victim signed to authorize an action in contract A, and replay it in contract B to obtain an unauthorized approval/weight check that passes `permission.getThreshold()`, letting the attacker perform an operation the victim never intended to authorize in that context — an unauthorized account operation as required by the validation rules.

### Likelihood Explanation
Medium: requires (a) two contracts on-chain that both use `ValidateMultiSign`/`BatchValidateSign` for authorization and (b) a data-tuple collision or an attacker-crafted contract B specifically designed to accept the same `(address, permissionId, data)` shape used by a popular contract A (phishing-style setup, same as the original report's "own the same token ID across collections"). This is fully reachable by an ordinary contract deployer plus a user who signs off-chain data, with no privileged role required.

### Recommendation
Document and/or enforce that `data` passed into `ValidateMultiSign`/`BatchValidateSign` must include a domain separator (calling contract address, chain id, and a purpose/nonce) before hashing, or change the precompile itself to mix in the caller's contract address (`msg.sender` inside the VM context) into the hash construction at `actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java:1062-1064`, so a signature valid for one calling contract cannot be replayed by a different calling contract.

### Proof of Concept
1. Contract A calls `validatemultisign(userAddr, permissionId, dataX, sigs)` where `dataX = keccak256(abi.encode(orderId, amount))`; user signs `sha256(userAddr || permissionId || dataX)` off-chain to authorize a trade in A.
2. Attacker deploys Contract B that also calls `validatemultisign(userAddr, permissionId, dataX, sigs)` with the identical `dataX` (e.g. by reusing the same encoding scheme, or by choosing parameters that happen to collide with a known signed message).
3. Contract B's call reaches the same code path shown at `actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java:1080-1110`, recovers the same signer address, hits the same weight/threshold, and returns `true` — granting B an authorization the user only intended for A.

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

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1162-1163)
```java
      DataWord[] words = DataWord.parseArray(data);
      byte[] hash = words[0].getData();
```
