## Analysis

The Sherlock report describes an unbounded `for` loop iterating over pool tokens in an oracle, where each iteration performs an external, non-trivial computation (`getPriceInEth`), and the number of iterations is attacker/pool-controllable, risking out-of-gas DoS.

The closest reachable analog in java-tron is the shielded-transaction validation path.

### Title
Unbounded loop over attacker-controlled Spend/Receive description lists causing expensive per-item zk-SNARK verification in `ShieldedTransferActuator` - (File: `actuator/src/main/java/org/tron/core/actuator/ShieldedTransferActuator.java`)

### Summary
`ShieldedTransferActuator.validate()` iterates over `spendDescriptions` and `receiveDescriptions` extracted directly from the broadcast `ShieldedTransferContract` and, inside `checkProof`, calls native zk-SNARK verification routines (`JLibrustzcash.librustzcashSaplingCheckSpend` / `librustzcashSaplingCheckOutput`) once per list element with no explicit cap on list size found in the actuator itself.

### Finding Description
`validate()` builds `spendDescriptions`/`receiveDescriptions` from the unpacked contract and passes them to `checkProof`: [1](#0-0) 

Inside `checkProof`, each `SpendDescription` and `ReceiveDescription` is verified individually via a native cryptographic call: [2](#0-1) 

No `MAX_SPEND`/`MAX_RECEIVE`-style guard was found in this actuator (a targeted `grep_search` for such constants returned no matches in this file besides the ones tied to `Wallet.java`, which is not enforced inside the actuator's own `validate()`/`execute()` path). Each entry drives an expensive elliptic-curve/zk-SNARK verification in native code, analogous to the reported pattern where each loop iteration invokes a costly external computation (`getPriceInEth`) whose cost scales with the attacker-controlled list length.

### Impact Explanation
If the number of `SpendDescription`/`ReceiveDescription` entries is not otherwise bounded before reaching this loop, a single broadcast transaction could force the node to perform many sequential native zk-SNARK verifications while processing/validating that transaction, increasing block-validation/transaction-processing latency disproportionately to the (bandwidth/energy) fee charged, which is a resource-exhaustion/DoS risk pattern matching the reported bug class (unbounded loop over an attacker-supplied collection driving expensive per-item external calls).

### Likelihood Explanation
Likelihood is constrained by two factors I could not fully verify within available context: (1) whether protobuf/transaction size limits (`Transaction` max size) or `calcFee` scaling by description count already bound the practical number of spend/receive descriptions per transaction, and (2) whether any upstream constant (referenced in `Wallet.java`, matched by the same grep but not inspected here) enforces a hard cap before the actuator is invoked. Because of these unresolved bounding mechanisms, I can't confirm this rises above the severity of the original report (which itself was rated only Medium, since Balancer pool token counts are also naturally capped by block gas). This should be treated as a plausible but not fully confirmed analog pending verification of the `Wallet.java` constants and transaction size limits.

### Recommendation
Add and enforce an explicit maximum count for `spend_description` and `receive_description` entries directly inside `ShieldedTransferActuator.validate()` (fail fast before any zk-SNARK verification work is performed), independent of any size limit enforced elsewhere in `Wallet.java`, to guarantee the actuator's own validation cost is bounded regardless of call path.

### Proof of Concept
Not independently reproducible from the indexed context alone — reproducing this would require confirming (a) the actual maximum number of `SpendDescription`/`ReceiveDescription` entries that fit within the enforced transaction size limit, and (b) whether the constants referenced in `Wallet.java` matching the same grep pattern are enforced before `ShieldedTransferActuator.validate()` is invoked. I recommend a Devin session with full repository access to inspect `Wallet.java`'s shielded-transaction-related constants and the transaction size/validation pipeline to confirm whether a cap already exists.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/ShieldedTransferActuator.java (L229-264)
```java
    List<SpendDescription> spendDescriptions = shieldedTransferContract.getSpendDescriptionList();
    // check duplicate sapling nullifiers
    if (CollectionUtils.isNotEmpty(spendDescriptions)) {
      HashSet<ByteString> nfSet = new HashSet<>();
      for (SpendDescription spendDescription : spendDescriptions) {
        if (nfSet.contains(spendDescription.getNullifier())) {
          throw new ContractValidateException("duplicate sapling nullifiers in this transaction");
        }
        nfSet.add(spendDescription.getNullifier());
        if (!merkleContainer.merkleRootExist(spendDescription.getAnchor().toByteArray())) {
          throw new ContractValidateException("Rt is invalid.");
        }
        if (nullifierStore.has(spendDescription.getNullifier().toByteArray())) {
          throw new ContractValidateException("note has been spend in this transaction");
        }
      }
    }

    List<ReceiveDescription> receiveDescriptions = shieldedTransferContract
        .getReceiveDescriptionList();

    HashSet<ByteString> receiveSet = new HashSet<>();
    for (ReceiveDescription receiveDescription : receiveDescriptions) {
      if (receiveSet.contains(receiveDescription.getNoteCommitment())) {
        throw new ContractValidateException("duplicate cm in receive_description");
      }
      receiveSet.add(receiveDescription.getNoteCommitment());
    }
    if (CollectionUtils.isEmpty(spendDescriptions)
        && CollectionUtils.isEmpty(receiveDescriptions)) {
      throw new ContractValidateException("no Description found in transaction");
    }

    //check spendProofs receiveProofs and Binding sign hash
    try {
      checkProof(spendDescriptions, receiveDescriptions, fee);
```

**File:** actuator/src/main/java/org/tron/core/actuator/ShieldedTransferActuator.java (L289-322)
```java
    if (CollectionUtils.isNotEmpty(spendDescriptions)
        || CollectionUtils.isNotEmpty(receiveDescriptions)) {
      long ctx = JLibrustzcash.librustzcashSaplingVerificationCtxInit();
      try {
        for (SpendDescription spendDescription : spendDescriptions) {
          if (!JLibrustzcash.librustzcashSaplingCheckSpend(
              new CheckSpendParams(ctx,
                  spendDescription.getValueCommitment().toByteArray(),
                  spendDescription.getAnchor().toByteArray(),
                  spendDescription.getNullifier().toByteArray(),
                  spendDescription.getRk().toByteArray(),
                  spendDescription.getZkproof().toByteArray(),
                  spendDescription.getSpendAuthoritySignature().toByteArray(),
                  signHash)
          )) {
            throw new ZkProofValidateException("librustzcashSaplingCheckSpend error", true);
          }
        }

        for (ReceiveDescription receiveDescription : receiveDescriptions) {
          if (receiveDescription.getCEnc().size() != ZC_ENCCIPHERTEXT_SIZE
              || receiveDescription.getCOut().size() != ZC_OUTCIPHERTEXT_SIZE) {
            throw new ZkProofValidateException("Cout or CEnc size error", true);
          }
          if (!JLibrustzcash.librustzcashSaplingCheckOutput(
              new CheckOutputParams(ctx,
                  receiveDescription.getValueCommitment().toByteArray(),
                  receiveDescription.getNoteCommitment().toByteArray(),
                  receiveDescription.getEpk().toByteArray(),
                  receiveDescription.getZkproof().toByteArray())
          )) {
            throw new ZkProofValidateException("librustzcashSaplingCheckOutput error", true);
          }
        }
```
