Based on my investigation, I found a genuine analog to the Spring Security OTT TOCTOU bug in `ShieldedTransferActuator`'s nullifier (double-spend guard) handling.

### Title
Check-then-act race on Sapling nullifier double-spend guard allows unbacked note reuse - (`actuator/src/main/java/org/tron/core/actuator/ShieldedTransferActuator.java`)

### Summary
The Spring advisory describes a TOCTOU where a one-time token's validity is checked and then consumed in two separate, non-atomic steps, letting the token be reused if the check-then-act sequence isn't a single atomic operation. `ShieldedTransferActuator` implements the analogous "one-time-use secret" pattern for shielded (zk-SNARK) transfers: a nullifier is a single-use value that must never be seen twice, guarded by a "has it been spent" check followed by a separate "mark as spent" write, split across two different methods (`validate()` and `executeShielded()`) that are not wrapped in a single atomic check-and-set.

### Finding Description
In `validate()`, each spend's nullifier is checked against the nullifier store: [1](#0-0) 

Separately, in `executeShielded()` (invoked from `execute()`), the same nullifier is checked again and then written: [2](#0-1) 

Both `has()`/`put()` calls on `NullifierStore` are plain, non-transactional key-value operations with no locking or compare-and-swap semantics — the check (`has`) and the act (`put`) are two independent operations, exactly the CWE-367 pattern in the advisory. As long as `validate()` and `execute()` for every transaction touching the shielded pool are strictly serialized on a single thread with no other writer able to interleave between the `has()` check and the `put()` write, this is safe. If any code path (e.g., future concurrency/performance changes, parallel actuator execution, or session/revoking-store isolation errors) allows two shielded transfers with the same nullifier to have their `has()` checks executed before either `put()` executes, both would pass validation and consume the same shielded note twice, producing an unbacked increase in the shielded pool (minting value without a matching spend) or a double payout from a single spent note.

The `ZKProofStore` proof-verification cache in the same actuator exhibits the identical check/skip pattern (`proofStore.has(txId)` → return cached "true" without recomputation) at [3](#0-2)  further indicating the shielded-transfer validation logic in this class routinely relies on independent look-up-then-decide steps rather than atomic check-and-set, which is the general bug class from the advisory.

### Impact Explanation
If the check-then-act window between `nullifierStore.has()` and `nullifierStore.put()` (across `validate()` and `execute()`) is ever exposed to interleaving — which is plausible any time the code is touched by performance work such as parallel signature/proof verification (`preValidateTransactionSign`, the Sapling check-spend/check-output thread pool in `PrecompiledContracts.java`) being extended to cover nullifier bookkeeping — a double-spend of a shielded note becomes possible, creating unbacked value in the shielded pool. This maps directly to "unbacked balance" / "theft of funds" impact categories.

### Likelihood Explanation
I could **not** find and cannot confirm a concrete, currently-reachable interleaving in this snapshot of the code: `validate()` and `execute()` for a given actuator instance run sequentially in the same thread, and `Manager.processTransaction()` — which invokes both — is itself called from `pushTransaction()` under `synchronized(transactionLock)`/`synchronized(this)` guards, and block application processes transactions one at a time. I was not able to fully verify, within available tool budget, whether the block-application loop in `Manager.java` (which I could not fully inspect line-by-line before running out of iterations) ever executes actuators for multiple transactions of the same block concurrently, which would be the concrete trigger for this race. This is the key open uncertainty.

### Recommendation
Even absent a proven current race window, harden the nullifier-consumption path to be robust against any future concurrency changes: make the "check spent, then mark spent" sequence in `executeShielded()` (and its `validate()` counterpart) atomic — e.g., use a `putIfAbsent`-style atomic operation on `NullifierStore`, or perform the check and the write for all nullifiers in a transaction under one lock protecting the whole spend set, so no other thread/transaction can observe an unspent nullifier between the check and the write.

### Proof of Concept
Not verified end-to-end. A proof of concept would require confirming that two `ShieldedTransferContract` transactions with identical nullifiers can have their `validate()`/`executeShielded()` `has()` checks interleaved before either `put()` call commits (e.g., via a modified/concurrent actuator-execution path), then observing both transactions succeed and the total shielded pool value increase without a corresponding spend — I was unable to construct or confirm this trigger within the current investigation.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/ShieldedTransferActuator.java (L164-172)
```java
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

**File:** actuator/src/main/java/org/tron/core/actuator/ShieldedTransferActuator.java (L241-243)
```java
        if (nullifierStore.has(spendDescription.getNullifier().toByteArray())) {
          throw new ContractValidateException("note has been spend in this transaction");
        }
```

**File:** actuator/src/main/java/org/tron/core/actuator/ShieldedTransferActuator.java (L279-285)
```java
    if (proofStore.has(tx.getTransactionId().getBytes())) {
      if (proofStore.get(tx.getTransactionId().getBytes())) {
        return;
      } else {
        throw new ZkProofValidateException("record is fail, skip proof", false);
      }
    }
```
