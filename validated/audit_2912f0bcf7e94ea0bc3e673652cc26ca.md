### Title
Missing domain/chain separator in `ValidateMultiSign`/`BatchValidateSign` TVM precompiles enables cross-chain signature replay - (File: `actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java`)

### Summary
The `ValidateMultiSign` (address `0x...0a`) and `BatchValidateSign` (address `0x...09`) TVM precompiled contracts let any deployed smart contract verify an off-chain-generated ECDSA/permission signature against an arbitrary caller-supplied hash. Neither precompile mixes any chain-identifying value (chain id, genesis block hash, or contract-domain separator) into the data that gets signed/hashed, so a signature that authorizes an action on one TRON-compatible chain is equally valid when replayed on another chain where the same account address and permission (or the same raw hash/address pair for `BatchValidateSign`) exist. This is the same bug class as the reported "Signature is replayable across domains" finding in CPortModule.sol/PaymentProcessor.sol, which was fixed by injecting a `domainSeparator` into the signed payload.

### Finding Description
`ValidateMultiSign.execute` builds the hash to be verified purely from caller-controlled/on-chain data with no chain binding: [1](#0-0) 
The combined preimage is `address || permissionId || data`, hashed with `Sha256Hash.hash`. This mirrors the vulnerable pattern in the report: the signature domain (i.e., which chain/deployment it is valid for) is never asserted — there is no chain id, genesis hash, or any other chain-unique value baked into the hash.

`BatchValidateSign` is even weaker: it lets the caller supply the exact hash to be checked with no binding at all: [2](#0-1) 
`words[0].getData()` is used verbatim as the message hash for `ECRecover`-style address recovery in `recoverAddrBySign`, so whatever a smart contract chooses to hash (often just business data with no domain separation) is exactly what gets signed off-chain by users.

By contrast, TRON's native transaction signature validation path binds the signed hash to the full raw transaction bytes (which include `ref_block_hash`/expiration/TAPOS fields tying it to a specific chain state) via `getTransactionId()`: [3](#0-2) 
That native path has implicit chain binding through TAPOS/block-hash references. The TVM precompiles have no equivalent protection — they are a general-purpose signature-verification primitive exposed to any deployed contract, and the contract author (not the protocol) is solely responsible for adding any domain separation, which most multisig/wallet contracts built on top of `validatemultisign`/`batchvalidatesign` do not do since the precompile's own hash construction gives no indication a domain separator is required.

Because TRON addresses are deterministic from the public key and often identical across networks that share the same genesis/key format (mainnet, Nile/Shasta testnets, private TRON forks, or any redeployed permission structure with the same address+permissionId), an attacker can:
1. Convince/observe a user signing a `validatemultisign` payload intended for one deployment/chain (e.g., a testnet multisig wallet, or a specific instance of a wallet contract).
2. Replay that exact signature against another deployment/chain where the same address holds an equivalent permission, or against another contract instance that happens to hash the same `(address, permissionId, data)` tuple, to satisfy the multisig threshold and trigger a privileged/financial action without the signer's consent for that specific context.

### Impact Explanation
Any smart contract built on top of `validatemultisign`/`batchvalidatesign` (a common pattern for on-chain multisig wallets, escrow, or governance contracts) inherits this replay weakness unless the contract author manually adds domain separation to the `data` field — something the precompile design does not encourage or enforce. This can lead to unauthorized approval of transactions/withdrawals on a different chain or contract instance than the signer intended, i.e., theft of funds or unauthorized privileged actions, which qualifies as Medium/High impact per the unauthorized-account-operation criterion.

### Likelihood Explanation
Reachable by any unprivileged account: any user can deploy a contract calling `validatemultisign`/`batchvalidatesign`, and any signer who signs a payload for use with one deployment can have that signature validated by a different contract/chain that reconstructs the identical hash. No privileged role, witness, or node compromise is required — only that two systems (chains or contract instances) compute the same hash for a given signature, which the precompile's own design makes trivially likely absent developer diligence.

### Recommendation
Mirror the report's fix pattern: require callers of `ValidateMultiSign`/`BatchValidateSign` (or the precompiles themselves) to mix a chain-unique domain separator (e.g., chain id / genesis block hash) and ideally the calling contract's own address into the hashed preimage, analogous to the `delegateCallReplacementDomainSeparator` fix applied to `PaymentProcessor.sol`. At minimum, document and strongly recommend that any contract relying on these precompiles include a domain separator (chain id + contract address) in the `data` it hashes before requesting signature verification, and consider adding a TIP to bind the precompile's internal hash construction to `chainId`/genesis hash by default in `PrecompiledContracts.java`.

### Proof of Concept
1. Deploy a wallet/multisig contract on Chain A that calls the `ValidateMultiSign` precompile with `(address, permissionId, data)`, per [1](#0-0) .
2. Have the same account (same address/permission structure) exist on Chain B (e.g., a testnet, private fork, or a second deployment reusing the same permission layout).
3. Obtain a signature the user produced for a `validatemultisign` call intended for Chain A's contract/action.
4. Submit the identical signature to Chain B's (or another contract instance's) `validatemultisign` call with the same `(address, permissionId, data)` triplet — since the hash formula contains no chain-unique value, the signature verifies successfully and the unintended action executes.

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

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1162-1163)
```java
      DataWord[] words = DataWord.parseArray(data);
      byte[] hash = words[0].getData();
```

**File:** chainbase/src/main/java/org/tron/core/capsule/TransactionCapsule.java (L663-667)
```java
      byte[] hash = getTransactionId().getBytes();

      long startNs = System.nanoTime();
      try {
        if (!validateSignature(this.transaction, hash, accountStore, dynamicPropertiesStore)) {
```
