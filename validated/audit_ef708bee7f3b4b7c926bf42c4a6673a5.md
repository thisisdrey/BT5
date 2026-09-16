### Title
`ValidateMultiSign` precompile computes signature hash without chain-domain separation, enabling cross-chain signature replay - ([File: actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java])

### Summary
The `ValidateMultiSign` precompiled contract (TVM address `0x0a`) lets any smart contract verify an off-chain, permission-based multisignature over arbitrary caller-supplied `data`. The message hash it verifies is built only from `address || permissionId || data`, with no chain identifier or domain separator mixed in, mirroring the reported Teller `attestLender` issue where a signature lacking `chainID` could be replayed on another chain.

### Finding Description
`ValidateMultiSign.execute` derives the hash to verify as: [1](#0-0) 

```
byte[] address = words[0].toTronAddress();
int permissionId = words[1].intValueSafe();
byte[] data = words[2].getData();
byte[] combine = ByteUtil.merge(address, ByteArray.fromInt(permissionId), data);
byte[] hash = Sha256Hash.hash(..., combine);
```

then recovers signer addresses from `signatures` against that `hash` and checks them against the account's `Permission` weights/threshold: [2](#0-1) 

There is no TRON/EVM chain ID, network magic, or any other domain-separation value folded into `combine`/`hash`. This precompile is reachable by any smart contract on the network (any unprivileged caller can deploy or invoke a contract that performs a `CALL` to address `0x0a`), so it is a fully permissionless attack surface, matching the same class of bug reported against Teller's `MarketRegistry`/`TellerASEIP712Verifier` (`keccak256` packing that omitted `chainID`).

The companion `BatchValidateSign` precompile (`0x09`) has the identical property — it verifies signatures over a caller-supplied 32-byte `hash` with no chain binding at all: [3](#0-2) 

Any dApp built on top of these precompiles to implement off-chain-signed authorizations (e.g., meta-transactions, permit-style approvals, order signing) inherits the missing chain-domain separation, because the account address and permission configuration (`Permission`/`Key` weights) can be identical across two java-tron-based networks (mainnet vs. a testnet/sidechain/fork sharing the same account state), which is exactly the "attested lender at one EVM chain can use same signature at another chain" scenario from the source report.

### Impact Explanation
If a dApp relies on `validatemultisign`/`batchvalidatesign` to gate a privileged operation (e.g., authorize a withdrawal, permission-based order, or approval) using an off-chain signature over `address/permissionId/data` (or a raw hash), an attacker who obtains a valid signature produced for one chain (e.g., Nile/Shasta testnet, or a private fork sharing the same account/permission setup) can replay it verbatim against the same contract deployed on another chain (e.g., mainnet), because nothing in the signed payload is chain-specific. This can result in unauthorized execution of privileged multisig-gated actions and, depending on the consuming contract, theft/unauthorized movement of funds — matching the Medium-severity impact of the original finding.

### Likelihood Explanation
Exploitation requires only: (1) a signature produced by a key holder for one java-tron network context, and (2) the same account address/permission structure existing (or attacker-influenceable) on a second chain instance that runs the identical contract logic consuming `ValidateMultiSign`/`BatchValidateSign`. This is a realistic and common pattern for TRON dApps (testnet/mainnet parity, chain forks, or multi-deployment DeFi protocols), and no privileged access is needed — any externally-owned account or contract can trigger the precompile call. Likelihood is Medium, consistent with the analog report's rating, since it depends on a consuming contract that reuses signed authorization data without adding its own chain-specific nonce/domain.

### Recommendation
Include a chain-domain separator in the hash computed by `ValidateMultiSign` (and document the same requirement for `BatchValidateSign` consumers): mix in the network's chain ID / genesis block hash into the `combine` buffer before hashing, e.g.: [4](#0-3) 
so that `combine = address || permissionId || chainId || data`. Additionally, document for dApp developers that `BatchValidateSign`'s raw-hash verification must itself be constructed by the calling contract to include a chain identifier, since the precompile provides no domain separation on its own.

### Proof of Concept
1. Deploy the same contract `C` (using `validatemultisign(address,uint256,bytes32,bytes[])`) on two java-tron networks, Chain A and Chain B, where the target account `addr` has an identical `Permission` (same `permissionId`, same keys/weights) on both chains — e.g., a standard multisig wallet address whose owner key signs a message for a withdrawal request `data`.
2. Owner signs `hash = SHA256(addr || permissionId || data)` off-chain and submits it once to `C` on Chain A to authorize action `data` (per `PrecompiledContracts.java:1062-1064,1108-1110`).
3. Attacker takes the identical `(addr, permissionId, data, signatures)` tuple and submits it to `C` deployed on Chain B. Because the hash formula has no chain-specific component, `ValidateMultiSign` on Chain B recomputes the same `hash`, recovers the same signer addresses, finds sufficient weight in the (attacker-unrelated but chain-B-existing) permission, and returns `dataOne()` (success) — authorizing the action on Chain B without a fresh signature from the owner.

### Citations

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1058-1064)
```java
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

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1162-1163)
```java
      DataWord[] words = DataWord.parseArray(data);
      byte[] hash = words[0].getData();
```
