### Title
Nullifiers are persisted to `NullifierStore` before all spend/receive descriptions in a shielded transaction are fully validated/committed, allowing a partial-failure state leak - (`File: actuator/src/main/java/org/tron/core/actuator/ShieldedTransferActuator.java`)

### Summary
`ShieldedTransferActuator.executeShielded()` writes each spend's nullifier directly into `NullifierStore` inside a loop *before* the Merkle-tree well-formedness check and before all receive commitments are inserted. If a later spend in the same list is found to be a double-spend, or if a subsequent `wfcheck()`/`saveCmIntoMerkleTree()` call throws `ZksnarkException`, `ContractExeException` is thrown - but the nullifiers already written for earlier spends in the loop are not explicitly rolled back inside the actuator itself.

### Finding Description
`executeShielded()` performs direct, unconditional writes to `nullifierStore` as it iterates the spend list: [1](#0-0) 

only afterwards does it perform the Merkle-tree well-formedness check and commitment insertion, both of which can independently throw and abort the transaction: [2](#0-1) 

This mirrors the CVE-2021-47389 bug class: a resource/context (`nullifierStore` entries) is created eagerly as a side effect, and the code path that can fail afterward does not explicitly undo that side effect before propagating the exception - relying entirely on an *external* mechanism (the caller's revoking-DB session) to discard the write. Unlike the SEV kernel bug, whether this actually results in a leak in java-tron depends on whether the actuator is always invoked inside a transaction-scoped revoking session that is discarded on any thrown exception. I was not able to fully confirm this within the available context/index — I could not locate the exact call site in `Manager`/`TransactionCapsule` that wraps `actuator.execute()` in a per-transaction session and reliably discards uncommitted `Snapshot` writes for every single-transaction/`validate()+execute()` invocation path (e.g., wallet's `pushTransaction` used for local relay, as opposed to `applyBlock`'s per-tx session in `Manager`). If any code path calls `validate()`/`execute()` without wrapping the underlying stores in a session that is rolled back on exception, the `nullifierStore.put()` calls for spends processed before the failure point would persist even though the overall transaction was rejected.

### Impact Explanation
If a rollback gap exists on some call path, the impact is that a client's spend note's nullifier becomes permanently marked as spent in `NullifierStore` even though the transaction that would have consumed it never took effect (no corresponding value was moved and no receive commitment was accepted). This would make the underlying shielded note permanently unspendable — a permanent freezing-of-funds condition for the note owner — without requiring any privileged access; a single crafted `ShieldedTransferContract` with multiple spend descriptions (one valid, one deliberately colliding with an already-spent nullifier, or a receive description crafted to fail `saveCmIntoMerkleTree`) is sufficient to trigger the code path.

### Likelihood Explanation
Likelihood is currently unproven at Medium/High confidence. The redundant `nullifierStore.has()` check inside `executeShielded()` largely duplicates checks already performed in `validate()`: [3](#0-2) 
so under the normal `validate()` → `execute()` invocation sequence on the same state snapshot, the double-spend branch inside `executeShielded()` should not be reachable. The more plausible failure trigger is the `wfcheck()`/`saveCmIntoMerkleTree()` `ZksnarkException` paths, which execute strictly after nullifiers for all spends have already been written. Whether this leaves permanent store corruption depends entirely on session/rollback semantics I could not fully verify with the tools available.

### Recommendation
Defer all `nullifierStore.put()` calls until after `wfcheck()` and all `saveCmIntoMerkleTree()` calls succeed (i.e., collect nullifiers to write and commit them only once the entire `executeShielded()` body succeeds), or perform the writes through a repository/deposit abstraction that only commits on full success (as `Program`/`VMActuator` do via `Repository.newRepositoryChild()` + `commit()`), so a mid-method failure cannot leave partially-applied nullifier state regardless of the caller's session handling. A Devin agent should also verify concretely whether `Manager`/`Wallet` wrap every `validate()+execute()` invocation of actuators in a discarding revoking session, to close out the uncertainty noted above.

### Proof of Concept
Not fully constructible without confirming the session/rollback behavior described above. A conceptual PoC would: 1) craft a `ShieldedTransferContract` with two `SpendDescription`s, the first spending a fresh, valid note, and the second reusing a nullifier from an already-processed prior transaction (bypassing the `validate()`-time duplicate check by exploiting a race between validation and execution, e.g. via reordering across blocks/forks), 2) submit the transaction so `executeShielded()` writes the first nullifier to `NullifierStore` before throwing `ContractExeException` on the second, and 3) check whether the first note's nullifier remains persisted in `NullifierStore` after the transaction is discarded, which would prove the note is now permanently unspendable despite the transaction never taking effect.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/ShieldedTransferActuator.java (L163-172)
```java
    //handle spends
    for (SpendDescription spend : spends) {
      if (nullifierStore.has(
          new BytesCapsule(spend.getNullifier().toByteArray()).getData())) {
        ret.setStatus(fee, code.FAILED);
        ret.setShieldedTransactionFee(fee);
        throw new ContractExeException("double spend");
      }
      nullifierStore.put(new BytesCapsule(spend.getNullifier().toByteArray()));
    }
```

**File:** actuator/src/main/java/org/tron/core/actuator/ShieldedTransferActuator.java (L174-194)
```java
    IncrementalMerkleTreeContainer currentMerkle = merkleContainer.getCurrentMerkle();
    try {
      currentMerkle.wfcheck();
    } catch (ZksnarkException e) {
      ret.setStatus(fee, code.FAILED);
      ret.setShieldedTransactionFee(fee);
      throw new ContractExeException(e.getMessage());
    }
    //handle receives
    for (ReceiveDescription receive : receives) {
      try {
        merkleContainer
            .saveCmIntoMerkleTree(currentMerkle, receive.getNoteCommitment().toByteArray());
      } catch (ZksnarkException e) {
        ret.setStatus(0, code.FAILED);
        ret.setShieldedTransactionFee(fee);
        throw new ContractExeException(e.getMessage());
      }
    }
    merkleContainer.setCurrentMerkle(currentMerkle);
  }
```

**File:** actuator/src/main/java/org/tron/core/actuator/ShieldedTransferActuator.java (L230-245)
```java
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
```
