I now have enough context to determine whether this bug class maps to a valid analog in java-tron.

The `ValidateMultiSign` precompiled contract (TVM address `0x0a`) is a general-purpose off-chain multisig verification utility exposed to any smart contract. It computes the message to be signed purely as `sha256(address || permissionId || arbitraryData)` with no chain identifier, no transaction reference, and no block binding whatsoever, just like the reported `CardTopupTrusted` bug class.

### Title
Off-chain multisig signatures verified by the `ValidateMultiSign`/`BatchValidateSign` TVM precompiles are replayable across forks and redeployments because the signed hash omits any chain identifier - (File: actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java)

### Summary
The `ValidateMultiSign` precompile (address `0x...0a`) lets any TVM contract verify an off-chain-collected multisig authorization for a given account/permission over caller-supplied `data`. The hash that signers actually sign is computed as `sha256(address || permissionId || data)`, with no chain ID, network magic, or any other domain separator tying the signature to a specific chain.

### Finding Description
In `ValidateMultiSign.execute`, the message hash bound to the signatures is constructed as: [1](#0-0) 

and each supplied signature is ECDSA-recovered against exactly that hash and checked against the target account's `Permission` keys/weights: [2](#0-1) 

Nothing in this input (`address`, `permissionId`, `data`) is derived from or bound to the chain the transaction executes on — there is no TRON chain ID, no genesis block hash, and no reference to any on-chain nonce or transaction hash. `BatchValidateSign` has the identical property: the caller supplies an arbitrary `bytes32 hash` directly and the precompile simply recovers signer addresses against it with no chain binding at all: [3](#0-2) 

This is architecturally the same root cause as the reported `CardTopupTrusted` issue: a trusted-party/multisig authorization scheme whose signed payload lacks a chain identifier, so a signature valid on one deployment is valid on every other deployment that shares the same account address, permission ID, and data/hash values.

### Impact Explanation
TRON is deployed identically across multiple independent networks (mainnet, Nile testnet, Shasta testnet) and has previously undergone consensus forks; any project using `ValidateMultiSign`/`BatchValidateSign` inside a smart contract to gate fund transfers, withdrawals, or governance actions based on off-chain-collected multisig approvals is vulnerable to replay of those exact same signatures on a different TRON-compatible chain/fork where the same account exists with the same permission configuration (e.g., an account whose keys/permissions were set up identically before a fork, or a contract redeployed with the same authorized signer set on a different network). This can lead to unauthorized execution of multisig-gated operations (theft of funds or unauthorized account operations) on the non-intended chain.

### Likelihood Explanation
Exploitation requires: (1) a downstream contract that relies on `ValidateMultiSign`/`BatchValidateSign` for authorization instead of native transaction-level multisig, and (2) the same address/permission/data (or same `hash`) being meaningful on two chains simultaneously (post-fork chains, or the same signer set deployed on multiple TRON-based networks). This is a realistic scenario for cross-chain bridges, sidechains, or projects that deploy identical governance/multisig contracts across TRON mainnet and other TRON-based networks (this is a documented use pattern for these precompiles), which is exactly the class of bug the original report flags.

### Recommendation
Include a chain identifier (e.g., a genesis block hash constant, or TRON's chain ID equivalent) as part of the hashed payload in both `ValidateMultiSign` (mixed into the `combine` byte array before hashing) and require/document that callers of `BatchValidateSign` incorporate chain-specific domain separation into the `hash` they pass in, following the EIP-712-style guidance referenced in the original report.

### Proof of Concept
1. Deploy identical contract code and an identical `Permission` configuration (same keys/weights) for address `A` on TRON mainnet and on a second TRON-based network (e.g., a private fork, testnet, or future hard-fork chain).
2. Off-chain, an authorized signer signs `sha256(A || permissionId || data)` intending to authorize an action only on chain 1.
3. An attacker captures this signature (e.g., from a public broadcast or bridge relay) and replays the identical `data` and signature bytes into `ValidateMultiSign` on chain 2, as demonstrated by the test harness pattern in [4](#0-3) .
4. Because the precompile's hash computation never references a chain ID, the signature verifies successfully on chain 2 as well, authorizing the same operation there without the signer's consent for that chain.

### Citations

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1062-1064)
```java
      byte[] combine = ByteUtil.merge(address, ByteArray.fromInt(permissionId), data);
      byte[] hash = Sha256Hash.hash(CommonParameter
          .getInstance().isECKeyCryptoEngine(), combine);
```

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1088-1109)
```java
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
```

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1162-1163)
```java
      DataWord[] words = DataWord.parseArray(data);
      byte[] hash = words[0].getData();
```

**File:** framework/src/test/java/org/tron/common/runtime/vm/ValidateMultiSignContractTest.java (L102-125)
```java
    //generate data

    byte[] address = key.getAddress();
    int permissionId = 2;
    byte[] data = Sha256Hash.hash(CommonParameter
        .getInstance().isECKeyCryptoEngine(), longData);

    //combine data
    byte[] merged = ByteUtil.merge(address, ByteArray.fromInt(permissionId), data);
    //sha256 of it
    byte[] toSign = Sha256Hash.hash(CommonParameter
        .getInstance().isECKeyCryptoEngine(), merged);

    //sign data

    List<Object> signs = new ArrayList<>();
    signs.add(Hex.toHexString(key1.sign(toSign).toByteArray()));
    //add Repetitive
    signs.add(Hex.toHexString(key1.sign(toSign).toByteArray()));
    signs.add(Hex.toHexString(key2.sign(toSign).toByteArray()));

    Assert.assertArrayEquals(
        validateMultiSign(StringUtil.encode58Check(key.getAddress()), permissionId, data, signs)
            .getValue(), DataWord.ONE().getData());
```
