### Title
Permanent DoS of Shielded (Sapling) Transfer Pool via Filling the Global Incremental Merkle Tree - (File: `chainbase/src/main/java/org/tron/common/zksnark/IncrementalMerkleTreeContainer.java`)

### Summary
`java-tron`'s shielded transaction subsystem (`ShieldedTransferContract`) uses a single, chain-wide incremental Merkle tree (`IncrementalMerkleTreeContainer`) with a fixed depth of `32`, limiting the tree to `2^32` note commitments. Once the tree is completely filled, `append()` unconditionally throws, and any subsequent shielded transaction that contains a receive/output note (i.e. any deposit into the shielded pool) will permanently fail, exactly mirroring the reported `L2ToL1MessagePasser` merkle-tree exhaustion bug.

### Finding Description
The tree depth is fixed via a static field: [1](#0-0) . Its `append` method enforces this hard cap and throws once the tree is complete: [2](#0-1) 

Every `ShieldedTransferContract` with at least one `ReceiveDescription` inserts its note commitment into this single global tree via `MerkleContainer.saveCmIntoMerkleTree`, which simply forwards to `tree.append(...)`: [3](#0-2) 

This is invoked unconditionally in `ShieldedTransferActuator.executeShielded` for every receive description in every shielded transaction broadcast by any unprivileged user: [4](#0-3) 

There is no alternate handling path, no tree rotation, and no mechanism to start a new tree once the current one is full — the same root cause described in the external report ("consider adding different handling for when a merkle tree is full").

### Impact Explanation
Once the single global Sapling note-commitment tree reaches its `2^32` leaf capacity, `IncrementalMerkleTreeContainer.append` throws `ZksnarkException("tree is full")` for every future receive, which `ShieldedTransferActuator.executeShielded` converts into a `ContractExeException`, failing the transaction. Because the tree is shared globally (there is only one "current"/"best" merkle tree per chain, tracked in `MerkleContainer`), this permanently and irrecoverably blocks the entire shielded pool feature — no user can ever again shield TRX/TRC10 into a new note, and no further "receive" side of a shielded transfer can be executed, effectively freezing the pool for future deposits while leaving spends of already-existing notes tied to a merkle root, which itself cannot be updated once full. This is a permanent Denial of Service on a chain feature reachable by any account broadcasting standard, unprivileged transactions — matching the required unauthorized-operation/permanent-freezing class of impact.

### Likelihood Explanation
Any unprivileged account can construct and broadcast `ShieldedTransferContract` transactions with receive descriptions (shielding TRX/tokens into the pool) using only the public wallet/gRPC API — no special privilege, no witness/committee/SR role is needed. As with the external report, exhausting `2^32` leaf slots requires a very large number of transactions, but the mechanism is deterministic, unconditional and requires no cooperation from any other party; it is only bounded by the attacker's willingness to pay bandwidth/energy fees over time, just like the acknowledged Sherlock finding it mirrors.

### Recommendation
Do not let the shielded pool's Merkle tree hit a hard, unrecoverable cap with no fallback. Options include: increasing the tree depth substantially beyond `32` to make exhaustion economically infeasible, or implementing tree rotation/chaining (start a new tree/root when full, similar to note commitment tree checkpoints in other Sapling-based systems) so `executeShielded` can gracefully continue processing shielded deposits instead of permanently failing.

### Proof of Concept
1. Repeatedly broadcast `ShieldedTransferContract` transactions, each containing at least one `ReceiveDescription` (a shielded deposit), from any account with sufficient TRX/energy to pay the shielding fee.
2. Each successful execution calls `ShieldedTransferActuator.executeShielded` → `MerkleContainer.saveCmIntoMerkleTree` → `IncrementalMerkleTreeContainer.append`, incrementing the single global tree's leaf count.
3. After `2^32` total leaves have been inserted (via automation over time, analogous to the original PoC's `~21,000,000` transactions), any further `ShieldedTransferContract` containing a `ReceiveDescription` will cause `append()` to throw `ZksnarkException("tree is full")`, which is caught and surfaced as `ContractExeException`, permanently failing every subsequent shielding transaction on the network. [5](#0-4)

### Citations

**File:** chainbase/src/main/java/org/tron/common/zksnark/IncrementalMerkleTreeContainer.java (L20-22)
```java
  @Getter
  @Setter
  private static Integer DEPTH = 32;
```

**File:** chainbase/src/main/java/org/tron/common/zksnark/IncrementalMerkleTreeContainer.java (L92-95)
```java
  public void append(PedersenHash obj) throws ZksnarkException {
    if (isComplete(DEPTH)) {
      throw new ZksnarkException("tree is full");
    }
```

**File:** chainbase/src/main/java/org/tron/common/zksnark/MerkleContainer.java (L83-89)
```java
  public IncrementalMerkleTreeContainer saveCmIntoMerkleTree(
      IncrementalMerkleTreeContainer tree, byte[] cm) throws ZksnarkException {
    PedersenHashCapsule pedersenHashCapsule = new PedersenHashCapsule();
    pedersenHashCapsule.setContent(ByteString.copyFrom(cm));
    tree.append(pedersenHashCapsule.getInstance());
    return tree;
  }
```

**File:** actuator/src/main/java/org/tron/core/actuator/ShieldedTransferActuator.java (L182-193)
```java
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
```
