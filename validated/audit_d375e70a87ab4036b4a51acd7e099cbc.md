### Title
Unbounded `depth` parameter passed directly to native Rust FFI in `MerkleHash` precompiled contract enables out-of-bounds access in native code - (File: actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java)

### Summary
The `MerkleHash` precompiled contract, reachable by any TVM `CALL`/`STATICCALL` from an unprivileged contract or EOA-triggered transaction, extracts a `level`/`depth` value directly from attacker-supplied calldata with **zero bounds validation** and passes it straight into the native JNI function `librustzcashMerkleHash`, which is backed by a Rust/C library.

### Finding Description
`PrecompiledContracts.MerkleHash#execute` reads the first 32-byte word of `data` as `level` via `parseInt(data)` and immediately forwards it, unchecked, to the native call: [1](#0-0) 

```
int level = parseInt(data);
System.arraycopy(data, 32, left, 0, 32);
System.arraycopy(data, 64, right, 0, 32);
JLibrustzcash.librustzcashMerkleHash(
    new LibrustzcashParam.MerkleHashParams(level, left, right, hash));
```
`parseInt` is only a `DataWord.intValueSafe()` cast of the raw calldata word — there is no check that `level` falls within the valid Merkle tree depth range (0–31, matching `TREE_WIDTH`/32-level Sapling tree used elsewhere).

Contrast this with every other call-site of the same native function in this class, where the `depth`/`level` argument is always derived from a bounded loop counter (`level - 1` for `level` in `[1, 32]`), because those call sites first validate `leafCount < TREE_WIDTH` and iterate a fixed-size `frontier[33][32]` array: [2](#0-1) [3](#0-2) 

The `MerkleHash` contract is the *only* caller that lets an external party supply the depth value directly, unconstrained, and pass it into native code: [4](#0-3) 

The JNI wrapper simply forwards the value to the native library without any Java-side sanity check: [5](#0-4) 

This is structurally identical to the FFmpeg CVE-2026-64831 pattern: a bitstream-derived count/index (`vps_num_hrd_parameters`) that should be bounded by `HEVC_MAX_SUB_LAYERS` is not validated before being used to index/write fixed-size arrays inside native decoder code. Here, an attacker-controlled `level` (which in the underlying Rust implementation is expected to index a fixed-size table of "uncommitted" Pedersen-hash generators/constants sized for a 32-level Sapling tree) is passed unchecked into native code, which — depending on how the Rust/C implementation handles the depth argument (e.g., indexing a fixed array of generator points or comparison table) — can result in out-of-bounds reads/writes across the JNI boundary. The Java layer's `try { ... } catch (Throwable any)` guard cannot catch or fully control a native crash or memory corruption that occurs inside the JNI call.

### Impact Explanation
Because `PrecompiledContracts` entries are invoked as ordinary TVM `CALL`/`STATICCALL` targets, any unprivileged smart-contract caller (deployer, or a contract invoked by an anonymous transaction/constant call) can trigger `MerkleHash` with a fully attacker-chosen `level` value with no upstream validation. If the native Rust implementation indexes a fixed-size table/array with `depth` without its own bounds check (mirroring the FFmpeg pattern where a downstream native routine trusted an unchecked count), this can cause:
- Node crash / denial of service (segfault in the JVM process hosting java-tron, taking down the full node — including block production if run on an SR), or
- Memory corruption in the native process, potentially leading to further exploitation (info leak or code execution) depending on how the native library lays out its internal tables.

Both outcomes (node crash/halt and native memory corruption) fall within the accepted impact categories (node crash/halt, potential RCE) for this analog.

### Likelihood Explanation
The path is trivially reachable: it requires only a single transaction/call from any account to the precompiled contract's fixed address (a `TriggerSmartContract`/`TriggerConstantContract` or an internal `CALL` from any deployed contract), with 96 bytes of attacker-controlled calldata. No special permissions, no signed contract deployment restrictions, and no witness/committee status are required — matching the "single signed transaction, contract call, or API request" bar. The only gate is availability of `MerkleHash` at its registered precompile address in `getContractForAddress`, which is present in the deployed `PrecompiledContracts` table (confirmed via the class's static contract dispatch, though the exact numeric address constant was not retrieved due to index truncation of that portion of the file).

### Recommendation
1. In `PrecompiledContracts.MerkleHash#execute`, validate `level` before calling into native code: reject (return failure/zero) if `level < 0 || level >= 32` (or whatever the true max Sapling tree depth is, consistent with `TREE_WIDTH` bounds enforced elsewhere in the same file).
2. Defense in depth: add the equivalent bounds check inside `JLibrustzcash.librustzcashMerkleHash` (Java wrapper) so all callers—current and future—are protected regardless of call site.
3. Audit the native Rust/C `librustzcash_merkle_hash` implementation itself to confirm/add a depth bound check on the native side, since Java-side validation alone does not protect against other, non-Java callers of the native library.

### Proof of Concept
Craft calldata for a direct call (via `TriggerConstantContract`/`TriggerSmartContract`, or from another contract's `CALL` opcode) to the `MerkleHash` precompiled contract address:
```
data = leftPad32(<attacker chosen "level", e.g. 0xFFFFFFFF or a large negative/huge value that decodes via intValueSafe() to something outside [0,31]>)
     + 32 bytes "left"
     + 32 bytes "right"
```
This reaches: [6](#0-5) 
with `level` fully attacker-controlled and unchecked, then flows into the native call in: [5](#0-4) 

Full confirmation that this reaches an actual native out-of-bounds condition requires inspecting the native Rust/C implementation of `librustzcash_merkle_hash`, which is outside the scope of the available Java source index; this report identifies the missing input-validation gap on the java-tron side that removes the last line of defense before untrusted input reaches native code.

### Citations

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1296-1343)
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
```

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1412-1418)
```java
        for (int i = 0; i < 33; i++) {
          System.arraycopy(data, i * 32 + 416, frontier[i], 0, 32);
        }
        long leafCount = parseLong(data, 1472);
        if (leafCount >= TREE_WIDTH) {
          return Pair.of(true, DataWord.ZERO().getData());
        }
```

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1799-1819)
```java
    public Pair<Boolean, byte[]> execute(byte[] data) {
      byte[] left = new byte[32];
      byte[] right = new byte[32];
      byte[] hash = new byte[32];
      boolean res = true;
      try {
        int level = parseInt(data);
        System.arraycopy(data, 32, left, 0, 32);
        System.arraycopy(data, 64, right, 0, 32);
        JLibrustzcash.librustzcashMerkleHash(
            new LibrustzcashParam.MerkleHashParams(level, left, right, hash));
      } catch (Throwable any) {
        res = false;
        logger.info("Compute MerkleHash failed:{}", any.getMessage());
      }
      if (res) {
        return Pair.of(true, hash);
      } else {
        return Pair.of(false, EMPTY_BYTE_ARRAY);
      }
    }
```

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1821-1824)
```java
    private int parseInt(byte[] data) {
      byte[] bytes = parseBytes(data, 0, 32);
      return new DataWord(bytes).intValueSafe();
    }
```

**File:** chainbase/src/main/java/org/tron/common/zksnark/JLibrustzcash.java (L208-211)
```java
  public static void librustzcashMerkleHash(MerkleHashParams params) {
    INSTANCE.librustzcashMerkleHash(params.getDepth(), params.getA(), params.getB(),
        params.getResult());
  }
```
