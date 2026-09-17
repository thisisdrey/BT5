### Title
Missing chain identifier in `ValidateMultiSign` precompile's signed-hash construction enables cross-chain signature replay - (File: `actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java`)

### Summary
The `ValidateMultiSign` TVM precompile constructs the message hash that off-chain signers are expected to sign entirely from `address`, `permissionId`, and caller-supplied `data`, with no chain identifier included. This mirrors the reported `claimBySignature` bug class: the protocol itself builds the signed payload and omits any chain-binding value, so a signature valid on one TRON-based network is equally valid on any other network sharing the same account address/permission layout.

### Finding Description
`ValidateMultiSign.execute()` builds the hash to verify as: [1](#0-0) 

```java
byte[] address = words[0].toTronAddress();
int permissionId = words[1].intValueSafe();
byte[] data = words[2].getData();

byte[] combine = ByteUtil.merge(address, ByteArray.fromInt(permissionId), data);
byte[] hash = Sha256Hash.hash(CommonParameter
    .getInstance().isECKeyCryptoEngine(), combine);
```

It then recovers each supplied signature against this hash and checks the recovered address's weight in the account's on-chain `Permission`: [2](#0-1) 

No `chainid`, genesis block hash, or any other network-specific value is folded into `combine`. This is functionally identical to the audited `claimBySignature` pattern in the report: `keccak256(abi.encodePacked(...))` without `block.chainid`. Here, TRON's own precompile — not application code — defines the exact hash format, so *every* smart contract built on top of `ValidateMultiSign` (e.g. multisig custody, escrow, bridge-release, and vault contracts that gate a fund transfer behind a threshold of `ValidateMultiSign` approvals) inherits this missing domain separation, since they have no way to add their own chain-id salt to what the precompile hashes internally.

TRON account addresses and their `Permission` key sets (active/multisig configuration) are portable across any TRON-derived network — mainnet, Nile/Shasta testnets, and private/forked java-tron deployments — because address derivation and permission-storage format are chain-agnostic. Consequently, if the same address/permissionId (and identical `data`, e.g. a withdrawal request hash reused between a staging/testnet deployment and mainnet, or between two independent java-tron based chains sharing a genesis/account setup) exists on two networks, a signature collected for approval on one chain is a fully valid `ValidateMultiSign` approval on the other.

### Impact Explanation
Any contract that relies on `ValidateMultiSign` to gate a state-changing action (fund release, ownership transfer, permission change) behind a threshold of off-chain signatures is exposed to cross-chain replay: an attacker who obtains a valid multisig approval package on one network can resubmit it via a `TriggerSmartContract` call on another TRON-compatible network where the same account/permission/data triple is present, and pass validation despite the signers never having intended the action on that chain. This can lead to unauthorized fund release or unauthorized state changes ("theft of funds" / unauthorized account operation) depending on how the calling contract wires the boolean approval result into asset movement.

### Likelihood Explanation
Exploitation requires (a) a contract built on `ValidateMultiSign` for multisig-gated actions to exist on more than one TRON-based network with the same account address/permission and identical `data` payload, and (b) an attacker to obtain a signature package from one network. Both preconditions are realistic for dApps commonly deployed identically across mainnet and testnets, or for permissioned/private java-tron forks (a common deployment model for this codebase), making this a real, reachable, if situational, risk rather than a purely theoretical one.

### Recommendation
Include a chain-specific domain separator (e.g. `ChainId` from the dynamic properties store, or the genesis block hash) in the `combine` byte array hashed by `ValidateMultiSign` (and consider doing the same for `BatchValidateSign`-style generic patterns used in first-party tooling), so a signature produced for one network cannot be replayed as a valid approval on another network sharing the same address/permission state.

### Proof of Concept
1. Deploy an identical multisig-gated vault contract (using `ValidateMultiSign` at precompile address `0x…a`) on two TRON-based networks (e.g. mainnet and a private java-tron fork/testnet), using the same funded account address and identical `Permission` (same signer keys/threshold).
2. Have the legitimate signers approve a withdrawal `data` payload on network A by signing `sha256(address || permissionId || data)` as computed in `ValidateMultiSign.execute()` (`actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java:1062-1064`).
3. Submit the same `address`, `permissionId`, `data`, and collected signatures via a `TriggerSmartContract` call to the vault contract on network B.
4. `ValidateMultiSign` recomputes the identical hash (no chain-id component) and returns success, since the signature/weight checks in `PrecompiledContracts.java:1088-1109` pass — the vault on network B releases funds/executes the action without the signers ever approving it for that network.

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
