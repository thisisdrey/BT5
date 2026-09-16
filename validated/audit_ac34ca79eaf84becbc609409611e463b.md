Based on my investigation, I found a strong analog in java-tron's `ValidateMultiSign` precompiled contract, which mirrors exactly the bug class in the report: an authorization signature whose signed payload omits any chain-identifying data, enabling cross-chain replay.

### Title
Cross-chain replay of `validatemultisign`/TVM permission signatures due to missing chain identifier in signed hash - (File: `actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java`)

### Summary
The `ValidateMultiSign` TVM precompile (exposed to Solidity as `validatemultisign(address,uint256,bytes32,bytes[])` at precompile address `0x0a`) lets any smart contract verify that a set of ECDSA signatures meets an account's permission threshold for an arbitrary, contract-supplied `data` hash. The signed message it reconstructs is `sha256(address ++ permissionId ++ data)` — it contains no chain identifier, network magic byte, or contract address binding beyond the TRON account address itself. [1](#0-0) 

### Finding Description
`ValidateMultiSign.execute()` builds the hash to be verified purely from `address` (the TRON account whose permission is being checked), `permissionId`, and an arbitrary 32-byte `data` value supplied by the calling contract: [2](#0-1) 

It then recovers signer addresses and sums permission-key weights against `account.getPermissionById(permissionId)` fetched from the current chain's `AccountStore`: [3](#0-2) 

This is structurally identical to the reported `Vault.changeRecipientAddress()` flaw: an off-chain-style signed authorization message that skips any chain-scoping value. Because the same TRON account address, permission structure, and key material can exist identically on multiple TRON-compatible networks (mainnet, testnet, and private/forked chains that share the same account model and key format), any dApp built on top of `validatemultisign` to authorize sensitive contract-level actions (analogous to `changeRecipientAddress`) is exposed to signature replay: a signature obtained for one network can be resubmitted verbatim to a contract on another network to satisfy the same permission-weight check, as long as the same address/permissionId/data triple is reused. The `BatchValidateSign` precompile (`batchvalidatesign`) has the same characteristic — the caller-supplied `hash` is verified as-is with no chain binding. [4](#0-3) 

Unlike the base `TransactionCapsule.checkWeight`/`validateSignature` path used for on-chain transactions — where the signed hash is the transaction ID derived from `raw_data` containing `ref_block_hash`/`ref_block_bytes` that are intrinsically bound to a specific chain's block history [5](#0-4)  — the `ValidateMultiSign`/`BatchValidateSign` precompiles provide a general-purpose signature-verification primitive to arbitrary smart contracts with no such chain-scoping, mirroring exactly the root cause identified in the external report (`keccak256(abi.encodePacked(msg.sender, _newRecipientAddress, expiry, address(this)))` missing chain id).

### Impact Explanation
Any contract deployer who builds a permission/authorization mechanism on top of `validatemultisign`/`batchvalidatesign` — e.g., off-chain-approved account operations, asset transfers, or recipient/owner changes gated by TRON account permission signatures — inherits a cross-chain replay weakness by design of the precompile: it offers no way to bind the signature to a specific chain. This can lead to unauthorized execution of privileged contract operations (theft of funds, unauthorized account/owner changes) on a second network reusing the same address/permission setup, without needing any new private key compromise.

### Likelihood Explanation
Reachable by any contract deployer or TVM contract that calls the `validatemultisign`/`batchvalidatesign` precompiles — a standard, unprivileged, publicly documented TVM feature (gated only by `VMConfig.allowTvmSolidity059()`), not requiring any special privilege node role. Exploitability depends on the existence of a second TRON-compatible network sharing the same account/permission state (e.g., testnet/mainnet reuse, or a forked/sidechain deployment), which is a realistic and common occurrence in the TRON ecosystem.

### Recommendation
Include a chain-scoping value (e.g., `chainId`/genesis block hash from `DynamicPropertiesStore`) in the hash computed by `ValidateMultiSign.execute()` and `BatchValidateSign.doExecute()`, or clearly document that callers of `validatemultisign`/`batchvalidatesign` must embed their own chain-id/domain separator in the `data`/`hash` argument they pass in, and update tooling/ABI-facing docs (and possibly `AbiUtil`/related SDK helpers) to enforce this by default.

### Proof of Concept
1. Deploy a contract on TRON network A that uses `validatemultisign(accountAddr, permissionId, dataHash, signatures)` to authorize a sensitive action (e.g., change a recipient address), following the pattern in `ValidateMultiSignContractTest.testDifferentCase` [6](#0-5) .
2. An attacker/legit relayer obtains valid signatures over `sha256(address ++ permissionId ++ data)` for that account on network A.
3. Deploy an identical contract on network B where the same account address exists with the same (or a compatible) permission configuration.
4. Submit the same `(address, permissionId, data, signatures)` tuple to the network-B contract's `validatemultisign` call — since the precompile hash construction contains nothing distinguishing network A from network B, the signatures validate successfully and the privileged action executes on network B without the signer's consent for that chain.

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

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1080-1109)
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
```

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1162-1175)
```java
      DataWord[] words = DataWord.parseArray(data);
      byte[] hash = words[0].getData();

      if (VMConfig.allowTvmSelfdestructRestriction()) {
        int sigArraySize = words[words[1].intValueSafe() / WORD_SIZE].intValueSafe();
        int addrArraySize = words[words[2].intValueSafe() / WORD_SIZE].intValueSafe();
        if (sigArraySize > MAX_SIZE || addrArraySize > MAX_SIZE) {
          return Pair.of(true, DATA_FALSE);
        }
      }

      byte[][] signatures = VMConfig.allowTvmSelfdestructRestriction() ?
          extractSigArray(words, words[1].intValueSafe() / WORD_SIZE, data) :
          extractBytesArray(words, words[1].intValueSafe() / WORD_SIZE, data);
```

**File:** chainbase/src/main/java/org/tron/core/capsule/TransactionCapsule.java (L584-587)
```java
  private Sha256Hash getRawHash() {
    return Sha256Hash.of(CommonParameter.getInstance().isECKeyCryptoEngine(),
        this.transaction.getRawData().toByteArray());
  }
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
