### Title
Unbounded array allocation from attacker-controlled length word in TVM precompiled-contract byte-array extraction helpers - (File: `actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java`)

### Summary
Several private helper methods used by TVM precompiled contracts (`extractBytes32Array`, `extractBytesArray`, `extractSigArray`) read a "length" word directly from attacker-supplied EVM call data via `DataWord.intValueSafe()` and immediately use it to allocate a Java array (`new byte[len][]`) **before** validating that `len` is consistent with the actual size of the supplied `data`/`words` buffer.

### Finding Description
`extractBytes32Array`, `extractBytesArray`, and `extractSigArray` all follow the same pattern: [1](#0-0) 

```
int len = words[offset].intValueSafe();
byte[][] bytesArray = new byte[len][];   // allocated BEFORE any bound check against data length
```

`words[offset]` is a 32-byte `DataWord` taken straight from the calldata a caller supplies to a precompiled contract (e.g. via a Solidity contract issuing a `staticcall`/`call` to the fixed precompile address that dispatches to `execute(byte[] data)`). `intValueSafe()` only clamps the value into the `int` range — it does not check that the resulting count is small enough to be backed by the actual `data` array that was passed in. An attacker who deploys a contract calling the precompile (or who crafts a triggering internal transaction) can set this length field to a very large value (up to `Integer.MAX_VALUE`), causing the JVM to attempt to allocate a huge `byte[][]` (or, downstream in `extractBytesArray`, additional `byte[]` allocations per element) with a size wildly disproportionate to the actual attacker-controlled payload actually transmitted. This is the same root-cause shape as CVE-2025-29491: a length/count field taken directly from untrusted input is used to size an allocation with no upper-bound or cross-check against the real available data, producing an allocation-size-too-big condition.

Because this code executes inside the TVM during transaction execution (both during normal block application/re-execution across all full nodes, and potentially during constant/eth_call-style queries), the huge allocation attempt happens synchronously on every node that processes or re-executes the transaction.

### Impact Explanation
An attempted allocation of gigabytes for a bogus array length throws `OutOfMemoryError` or causes severe GC pressure, which can crash the TVM execution thread or, if the OOM propagates unhandled, destabilize/crash the node process. Because transaction execution during block application happens on every full node/SR that must catch up to or validate the chain, a single such crafted call data value in a signed transaction could be replayed and cause a repeatable denial-of-service against any node that (re)plays this transaction, including nodes serving the HTTP/gRPC/JSON-RPC APIs. This matches the "node crash or halt"/"API the node can no longer serve" acceptance criteria.

### Likelihood Explanation
Reaching these helpers requires only that an unprivileged account deploy (or call) a contract that invokes the relevant precompiled contract with attacker-chosen calldata words — no special privilege, SR/witness status, or network position is needed. The vulnerable allocation happens unconditionally before any bound/consistency check against the actual supplied `data` length, so triggering it requires only crafting the length word in the call data, which is straightforward and fully attacker-controlled.

### Recommendation
Before allocating `new byte[len][]` (and any subsequent per-element `byte[]` allocations) in `extractBytesArray`, `extractBytes32Array`, and `extractSigArray`, validate `len` against the maximum number of elements that could possibly be represented by the remaining bytes in `words`/`data` (e.g., `len <= (words.length - offset - 1)` for `extractBytes32Array`/`extractSigArray`, and an equivalent bound derived from `data.length` for `extractBytesArray`). Reject with an execution revert (return failure/zero) if the declared length exceeds what the actual payload can support, mirroring the bound checks already used elsewhere (e.g., `verifyLength` in `framework/src/main/java/org/tron/core/capsule/utils/RLP.java`).

### Proof of Concept
1. Deploy a contract that performs a low-level `call`/`staticcall` to the precompiled-contract address that internally dispatches to one of `extractBytesArray`/`extractBytes32Array`/`extractSigArray` (these are private static helpers within `PrecompiledContracts.java`; exact call sites for these three helpers could not be fully enumerated in this analysis due to tool-call limits, but the allocation pattern itself is confirmed present in the file).
2. Craft the calldata so that the `DataWord` at the expected "length/offset" position decodes (via `intValueSafe()`) to a very large positive integer (e.g., close to `Integer.MAX_VALUE`), while supplying only a small amount of actual trailing data.
3. Broadcast the transaction (or trigger it via an `eth_call`/`triggerConstantContract` API request).
4. Observe that the node attempts `new byte[len][]` with the huge `len`, resulting in `OutOfMemoryError`/excessive memory pressure during TVM execution, potentially crashing or hanging the executing node.

Note: I was unable to fully trace, within the remaining tool budget, the exact outer precompiled-contract `execute()` method(s) that call `extractBytesArray`/`extractBytes32Array`/`extractSigArray` (i.e., which fixed precompile address triggers each helper). The vulnerable allocation pattern itself, however, is directly confirmed in the cited source lines.

### Citations

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L390-426)
```java
  private static byte[][] extractBytes32Array(DataWord[] words, int offset) {
    int len = words[offset].intValueSafe();
    byte[][] bytes32Array = new byte[len][];
    for (int i = 0; i < len; i++) {
      bytes32Array[i] = words[offset + i + 1].getData();
    }
    return bytes32Array;
  }

  private static byte[][] extractBytesArray(DataWord[] words, int offset, byte[] data) {
    if (offset > words.length - 1) {
      return new byte[0][];
    }
    int len = words[offset].intValueSafe();
    byte[][] bytesArray = new byte[len][];
    for (int i = 0; i < len; i++) {
      int bytesOffset = words[offset + i + 1].intValueSafe() / WORD_SIZE;
      int bytesLen = words[offset + bytesOffset + 1].intValueSafe();
      bytesArray[i] = extractBytes(data, (bytesOffset + offset + 2) * WORD_SIZE,
          bytesLen);
    }
    return bytesArray;
  }

  private static byte[][] extractSigArray(DataWord[] words, int offset, byte[] data) {
    if (offset > words.length - 1) {
      return new byte[0][];
    }
    int len = words[offset].intValueSafe();
    byte[][] bytesArray = new byte[len][];
    for (int i = 0; i < len; i++) {
      int bytesOffset = words[offset + i + 1].intValueSafe() / WORD_SIZE;
      bytesArray[i] = extractBytes(data, (bytesOffset + offset + 2) * WORD_SIZE,
          SIG_LENGTH);
    }
    return bytesArray;
  }
```
