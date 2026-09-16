### Title
`ValidateMultiSign`/`BatchValidateSign` precompiles let smart contracts define arbitrary attacker-controlled signing data with no domain separation, enabling cross-contract/cross-chain signature replay - (File: `actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java`)

### Summary
The TVM precompiled contracts `ValidateMultiSign` (address `0x...0a`) and `BatchValidateSign` (address `0x...09`) expose a generic "check if this signature authorizes this account/permission over this data" primitive to any TRC contract. The message that gets hashed and checked against the signature is built purely from attacker/contract-supplied fields (`address`, `permissionId`, `data`) with no chain ID, no calling-contract address, and no type/domain separator, exactly the bug class described in the referenced report (arbitrary data used as "signature", enabling replay across contexts).

### Finding Description
`ValidateMultiSign.execute` builds the signed message as: [1](#0-0) 

```
DataWord[] words = DataWord.parseArray(rawData);
byte[] address = words[0].toTronAddress();
int permissionId = words[1].intValueSafe();
byte[] data = words[2].getData();

byte[] combine = ByteUtil.merge(address, ByteArray.fromInt(permissionId), data);
byte[] hash = Sha256Hash.hash(CommonParameter.getInstance().isECKeyCryptoEngine(), combine);
```

`address`, `permissionId`, and `data` are all values passed in by whatever calling smart contract invokes the precompile — none of them are pinned to msg.sender/the caller's own state, and the resulting `hash` has:
- no domain separator tying it to the calling contract's address,
- no chain ID,
- no type hash identifying the purpose of the signature,
- `data` is a fully attacker/contract-defined 32-byte blob (`words[2].getData()`).

The precompile then recovers the signer for each supplied signature and checks it against the target account's `Permission` weights: [2](#0-1) 

This means:
1. Any DApp author can build a Solidity contract that asks users to sign `sha256(address ++ permissionId ++ data)` for some business purpose and calls `validatemultisign`/`batchvalidatesign` to check the signature on-chain.
2. Because there is no domain separator including the consuming contract's own identity, chain ID, or an action-specific type hash, a signature a user produced for one DApp's `data` value is equally valid input to `ValidateMultiSign` when replayed by a different (possibly malicious) contract that happens to construct the same `address`/`permissionId`/`data` triple, or on any other java-tron-based chain (e.g. mainnet vs. a fork/private chain) using the same account address and permission id.
3. `BatchValidateSign` has the analogous problem: its callers supply a raw `hash` directly, so it is not even a domain-separated construction — any bytes32 the contract wants can be checked as "signed" by an address, with no notion of what the signature was originally intended to authorize.

This is the identical bug class from the report: bytes-based, non-EIP-712 signature verification with no domain separation, exposed at a layer (a system precompile callable from any TVM contract) that any contract deployer or contract-calling user can reach.

### Impact Explanation
Because this primitive is exposed generically to every smart contract on the chain, a malicious contract author can craft a scheme where they solicit a signature from a victim under a plausible pretext, then feed the exact same `(address, permissionId, data)` triple (or in `BatchValidateSign`, the exact same `hash`) into `validatemultisign`/`batchvalidatesign` to have the precompile "confirm" authorization for a different, unintended action against the victim's account permission — e.g. gating a fund transfer or privileged operation in the calling contract's own logic. Since Tron addresses/permissions are shared across java-tron-based networks (mainnet, testnets, private chains) and across all contracts on a chain, a signature harvested in one context can be replayed in another, leading to unauthorized authorization of contract-gated operations tied to a real Tron account's permission set — a concrete unauthorized-account-operation / fund-theft vector when a DApp relies on this precompile as its sole authorization check.

### Likelihood Explanation
Reaching this code requires nothing more than deploying and calling a smart contract (an unprivileged, permissionless action) that invokes address `0x...0a` or `0x...09`; both are enabled once `VMConfig.allowTvmSolidity059()` is active, which is already the case on production networks. Exploitation additionally requires a victim to have produced a signature over the vulnerable format for some purpose and an attacker contract able to reconstruct the identical `(address, permissionId, data)`/`hash` — a realistic scenario for any DApp ecosystem built on top of this primitive (analogous exactly to the referenced report's `matchOrder`/`buyPosition` scenario), since Tron actively documents and encourages this precompile for building "multisig" gated DApps.

### Recommendation
Add a domain separator to the hashed payload inside `ValidateMultiSign`/`BatchValidateSign` (or provide a new, EIP-712-style precompile) that includes at minimum: the chain ID (`VMConfig`/`ChainConstant` chain id), the calling contract's address (the actual `msg.sender`/`CALLER` invoking the precompile, not merely a caller-supplied `address` field), and a type hash describing the semantics of `data`, so the same signed payload cannot be replayed across differing contracts, purposes, or chains. Document to DApp developers that they must not treat this primitive as a domain-separated signature scheme on its own.

### Proof of Concept
1. A DApp `A` on java-tron asks user `U` to off-chain sign `sha256(U.address ++ 2 ++ data)` (data = an application-specific payload) and calls `validatemultisign(U.address, 2, data, [sig])` to authorize some action gated on `U`'s active permission id 2, per the exact flow exercised in [3](#0-2) .
2. A malicious contract `B` (deployed by anyone) observes/derives the same `data` value (or is designed so its own protected `data` field is made to coincide with `A`'s) and calls the same precompile `validatemultisign(U.address, 2, data, [sig])` to pass its own authorization check gating a privileged operation against `U`'s account/permission, reusing `sig` that was never intended for `B`.
3. Because `hash = sha256(address ++ permissionId ++ data)` contains no chain ID, no reference to contract `B`'s address, and no type hash distinguishing "authorize A's action" from "authorize B's action," the check in `ValidateMultiSign.execute` (`PrecompiledContracts.java:1088-1110`) succeeds for `B`, granting unauthorized access to whatever action `B` gates behind this call.

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
