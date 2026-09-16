Based on my research, I found a plausible analog vulnerability in the `ValidateMultiSign`/`BatchValidateSign` TVM precompiled contracts, which mirrors the core failure mode of the NuCypher report: a signature verification scheme with insufficient domain binding, allowing signatures to be replayed outside their intended scope.

### Title
Missing domain separation and nonce binding in `ValidateMultiSign`/`BatchValidateSign` TVM precompiles enables cross-contract signature replay - ([File: actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java])

### Summary
The `validatemultisign` (address `0x0a`) and `batchvalidatesign` (address `0x09`) precompiled contracts let any TVM smart contract verify that an account's active/multi-sig permission has approved an arbitrary caller-supplied 32-byte value. The hash that is actually checked against the ECDSA signature is computed as `sha256(address || permissionId || data)`, with no chain id, no calling-contract address, and no nonce/expiration baked in by the protocol itself.

### Finding Description
In `PrecompiledContracts.ValidateMultiSign.execute`, the verified hash is built purely from the target account's address, its permission id, and a `bytes32 data` value supplied by the calling contract's calldata: [1](#0-0) 
Weights are then summed against `TransactionCapsule.getWeight(permission, recoveredAddr)` and compared to `permission.getThreshold()`. Unlike a normal on-chain transaction — whose signature covers `Transaction.raw` including `expiration`, `ref_block_hash`/`ref_block_bytes` (TAPoS) and is additionally deduplicated by `Manager.validateDup`/`containsTransaction` using the transaction id as a global replay guard [2](#0-1)  — the `data` hash passed into this precompile carries none of that protocol-level context. The precompile itself performs no bookkeeping of previously used `(address, permissionId, data, signature)` tuples, so nothing prevents the same off-chain signature from being replayed:
- against a different smart contract that happens to expect the same encoded `data` value (no verifying-contract address in the hash), or
- repeatedly within the same contract, unless that specific contract's Solidity code manually tracks a nonce — a responsibility left entirely to third-party contract authors, exactly like the NuCypher policy code left `m`, nonces and revocation to ad-hoc application logic instead of enforcing it at the protocol layer.

This is structurally the same class of bug described in the report: a piece of data that materially changes the meaning/scope of an approval (there: `m`/kfrags in a policy; here: which contract/action the multisig approval is meant to authorize) is not included in what is actually signed and checked, and there is no built-in replay/nonce tracking to compensate.

### Impact Explanation
Any dApp built on top of `validatemultisign`/`batchvalidatesign` for gating privileged operations (e.g., withdrawal approval, governance execution, custodial multisig wallets) can have a previously valid, observed signature reused to authorize unauthorized actions in a different contract or a second time in the same contract if that contract's own logic does not add its own nonce/domain separation. Because the precompile is billed as validating a "multi-sign" approval, contract authors reasonably expect the underlying primitive to enforce basic replay resistance for the account/permission it operates on, similar to what full on-chain transactions get for free from TAPoS + dup-transaction checks. Successful exploitation leads to unauthorized account operations / theft of funds in affected contracts.

### Likelihood Explanation
Reachable by any unprivileged smart-contract deployer or transaction sender: the precompile is exposed to all TVM contracts once `VMConfig.allowTvmSolidity059()` is enabled [3](#0-2) , requiring no special privileges, and the vulnerable pattern (signing/using a bare hash without contract/chain/nonce binding) is a common integration mistake because Tron's documentation and precedent (e.g., Ethereum's `ecrecover`) do not make the domain-separation requirement obvious.

### Recommendation
Document explicitly (and consider enforcing) that callers of `validatemultisign`/`batchvalidatesign` must include a domain separator (chain id, verifying contract address, and a nonce/expiration) inside the `data` they hash before requesting signatures, mirroring the recommendation in the original report to require a nonce/signature scope per authorization and to track used nonces to prevent replay.

### Proof of Concept
1. Deploy `ContractA`, which calls `validatemultisign(alice, permissionId, dataHash, sigs)` to authorize "transfer 100 TRX to X" where `dataHash = sha256(payload)`.
2. Alice signs `sha256(alice || permissionId || dataHash)` off-chain and submits it to `ContractA`, which executes the transfer.
3. Deploy `ContractB` with unrelated logic that also happens to call `validatemultisign(alice, permissionId, dataHash, sigs)` for a different, higher-value action but reuses the same `dataHash` encoding convention.
4. An attacker who observed step 2's calldata/signature on-chain resubmits the same `sigs` to `ContractB`; `ValidateMultiSign.execute` recomputes the identical hash and returns success, since neither the precompile nor the signature covers which contract or which specific action is authorized, allowing the attacker to trigger `ContractB`'s privileged action without a fresh approval from Alice.

### Citations

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L254-259)
```java
    if (VMConfig.allowTvmSolidity059() && address.equals(batchValidateSignAddr)) {
      return batchValidateSign;
    }
    if (VMConfig.allowTvmSolidity059() && address.equals(validateMultiSignAddr)) {
      return validateMultiSign;
    }
```

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

**File:** framework/src/main/java/org/tron/core/db/Manager.java (L861-881)
```java
  void validateDup(TransactionCapsule transactionCapsule) throws DupTransactionException {
    if (containsTransaction(transactionCapsule)) {
      throw new DupTransactionException(String.format("dup trans : %s ",
          transactionCapsule.getTransactionId()));
    }
  }

  private boolean containsTransaction(TransactionCapsule transactionCapsule) {
    return containsTransaction(transactionCapsule.getTransactionId().getBytes());
  }


  private boolean containsTransaction(byte[] transactionId) {
    if (transactionCache != null && !transactionCache.has(transactionId)) {
      // using the bloom filter only determines non-existent transaction
      return false;
    }

    return chainBaseManager.getTransactionStore()
        .has(transactionId);
  }
```
