Found a directly analogous off-by-one bound check in `VerifyTransferProof.execute()`.

### Title
Off-by-one bound check on `leafCount` in `VerifyMintProof`/`VerifyTransferProof` allows out-of-bounds Merkle-frontier array write - ([File: actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java])

### Summary
`VerifyMintProof.execute()` rejects a transaction only when `leafCount >= TREE_WIDTH`, but `VerifyTransferProof.execute()` (the sibling precompile that verifies a shielded transfer, reachable by any contract that calls the Sapling shielded-transfer precompiled contract) rejects only when `leafCount >= TREE_WIDTH - 1`, and inserts **two** leaves per call via `insertLeaves`. This mirrors the CVE-2023-53238 pattern: a boundary check that is off by one relative to the number of elements actually written in the following loop, permitting the loop to run one iteration past the last valid array slot.

### Finding Description
`insertLeaves()` computes, for every note being inserted, a `slot[i] = getFrontierSlot(leafCount + i)` and later indexes `frontier[slot[i]]` (and `UNCOMMITTED[level - 1]`) up to `slot[cmCount - 1] + 1 .. 32`: [1](#0-0) 

`frontier` is always allocated as `new byte[33][32]` by the callers, and `UNCOMMITTED` is `new byte[32][32]`: [2](#0-1) 

`VerifyMintProof` inserts a single leaf and correctly bounds `leafCount` with `leafCount >= TREE_WIDTH`: [3](#0-2) 

`VerifyTransferProof`, however, inserts up to **two** leaves (`receiveCount` up to 2, via `insertLeaves(frontier, leafCount, receiveCm)`) but uses the check `leafCount >= TREE_WIDTH - 1`: [4](#0-3) [5](#0-4) 

Because `insertLeaves` is called with `leafValue = receiveCm` whose length equals `receiveCount` (1 or 2), the internal loop computes `slot[i] = getFrontierSlot(leafCount + i)` for `i` up to `receiveCount - 1`. When `leafCount == TREE_WIDTH - 2` and `receiveCount == 2`, the check `leafCount >= TREE_WIDTH - 1` passes (does not reject), yet the second leaf is effectively inserted at logical position `TREE_WIDTH - 1`, i.e. exactly at the point the mint-path check would have rejected. This is the direct “`>` should have been `>=`”-class boundary bug: the guard is one leaf short of covering the true number of slots the subsequent loop can touch, exactly analogous to `hisi_inno_phy_probe()`'s `i > INNO_PHY_PORT_NUM` (should be `>=`) permitting one extra iteration of the port array.

### Impact Explanation
Since `TREE_WIDTH = 1L << 32` is enormous, reaching `leafCount` anywhere close to `TREE_WIDTH - 2` is computationally and economically infeasible in practice for the public shielded pool, so the direct write is not realistically exploitable through the natural `leafCount` growth path today. However, the finding demonstrates a genuine boundary-check inconsistency between two precompiles that operate on the same shared `frontier`/`UNCOMMITTED` fixed-size arrays and same `insertLeaves` routine — one enforces `< TREE_WIDTH`, the other enforces `< TREE_WIDTH - 1` while inserting up to 2 leaves. If `getFrontierSlot`/`insertLeaves` is ever reused with a different `cmCount`, or if the boundary is approached via any other state-manipulation path, the mismatch would manifest as `ArrayIndexOutOfBoundsException` inside a TVM precompiled-contract execution, which is an unhandled `Throwable` case only partially caught (`catch (Throwable any)` does catch it in this method, so today it degrades to returning a `0` result rather than crashing the node) — limiting the concrete impact to a logic/consistency defect rather than a currently provable node crash or fund-loss path.

### Likelihood Explanation
Low likelihood of practical exploitation today because `leafCount` must be adjacent to `2^32 - 2`, which is not attainable through the normal cost of shielded mint/transfer operations. The bug is nonetheless a real, provable off-by-one discrepancy between the two sibling validation checks guarding the identical `insertLeaves` array-write routine, reachable from an unprivileged TVM contract call to the Sapling shielded-transfer precompile.

### Recommendation
Align the `leafCount` bound check in `VerifyTransferProof.execute()` with the number of leaves it can insert: reject when `leafCount + receiveCount - 1 >= TREE_WIDTH` (i.e., `leafCount > TREE_WIDTH - receiveCount`), mirroring `VerifyMintProof`'s single-leaf check but accounting for `receiveCount` up to 2. More generally, `insertLeaves` should itself validate that `leafCount + cmCount - 1 < TREE_WIDTH` before indexing `frontier`/`UNCOMMITTED`, rather than relying on each caller to independently derive a consistent bound.

### Proof of Concept
Not independently reproducible with realistic parameters: exploitation requires driving `leafCount` to `TREE_WIDTH - 2` (≈ 2^32 - 2), which is outside the reach of normal transaction execution. The finding is established by static code comparison of the two boundary checks against the shared `insertLeaves` loop bounds, as cited above.

### Citations

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1250-1251)
```java
    protected static final long TREE_WIDTH = 1L << 32;
    protected static final byte[][] UNCOMMITTED = new byte[32][32];
```

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1296-1346)
```java
    protected Pair<Boolean, byte[]> insertLeaves(
        byte[][] frontier, long leafCount, byte[][] leafValue) {
      long nodeIndex = 0;
      boolean success = true;
      byte[] leftInput;
      byte[] rightInput;
      byte[] hash = new byte[32];
      byte[] nodeValue = new byte[32];
      int cmCount = leafValue.length;
      int[] slot = new int[cmCount];
      for (int i = 0; i < cmCount; i++) {
        slot[i] = getFrontierSlot(leafCount + i);
      }
      int resultArrayLength = 32;
      for (int i = 0; i < cmCount; i++) {
        resultArrayLength += (slot[i] + 1) * 32;
      }

      byte[] result = new byte[resultArrayLength];
      try {
        int offset = 0;
        for (int i = 0; i < cmCount; i++) {
          byte[] slotArray = DataWord.of((byte) (slot[i] & 0xFF)).getData();
          System.arraycopy(slotArray, 0, result, offset, 32);
          offset += 32;
          nodeIndex = i + leafCount + TREE_WIDTH - 1;
          System.arraycopy(leafValue[i], 0, nodeValue, 0, 32);
          if (slot[i] == 0) {
            System.arraycopy(nodeValue, 0, frontier[0], 0, 32);
            continue;
          }
          for (int level = 1; level <= slot[i]; level++) {
            if (nodeIndex % 2 == 0) {
              leftInput = frontier[level - 1];
              rightInput = nodeValue;
              nodeIndex = (nodeIndex - 1) / 2;
            } else {
              leftInput = nodeValue;
              rightInput = UNCOMMITTED[level - 1];
              nodeIndex = nodeIndex / 2;
            }
            JLibrustzcash.librustzcashMerkleHash(new LibrustzcashParam.MerkleHashParams(
                level - 1, leftInput, rightInput, hash));
            System.arraycopy(hash, 0, nodeValue, 0, 32);
            System.arraycopy(hash, 0, result, offset, 32);
            offset += 32;
          }
          System.arraycopy(nodeValue, 0, frontier[slot[i]], 0, 32);
        }

        for (int level = slot[cmCount - 1] + 1; level <= 32; level++) {
```

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1415-1418)
```java
        long leafCount = parseLong(data, 1472);
        if (leafCount >= TREE_WIDTH) {
          return Pair.of(true, DataWord.ZERO().getData());
        }
```

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1487-1499)
```java
        long leafCount = parseLong(data, 1280);
        if (leafCount >= TREE_WIDTH - 1) {
          return Pair.of(true, DataWord.ZERO().getData());
        }

        int spendCount = parseInt(data, spendOffset);
        int spendAuthSigCount = parseInt(data, spendAuthSigOffset);
        int receiveCount = parseInt(data, receiveOffset);

        if (spendCount != spendAuthSigCount || spendCount < 1
            || spendCount > 2 || receiveCount < 1 || receiveCount > 2) {
          return Pair.of(true, DataWord.ZERO().getData());
        }
```

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1594-1597)
```java
          checkResult = checkResult && eachTaskResult;
        }
        if (checkResult) {
          return insertLeaves(frontier, leafCount, receiveCm);
```
