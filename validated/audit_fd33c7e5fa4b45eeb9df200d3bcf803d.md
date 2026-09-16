### Title
Missing bounds validation on attacker-controlled length/offset fields when parsing `DataWord[]` arrays for TVM precompiled contracts causes out-of-bounds array access - (File: `actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java`)

### Summary
The helper methods `extractBytes32Array` and `extractBytesArray` in `PrecompiledContracts.java` decode variable-length arrays out of a `DataWord[] words` buffer that is built from the raw calldata of a TVM precompiled-contract call. Both methods trust a length/offset value read directly from that attacker-supplied calldata to define how many subsequent elements of `words` to read, without ever checking that the resulting index stays inside the bounds of `words`. This mirrors the root cause of CVE-2020-15201: a `splits`-style value taken from untrusted input is assumed to validly partition a backing array, and the code walks past the end of that array when the assumption is violated.

### Finding Description
`extractBytes32Array` reads a count from the first word and then unconditionally indexes `words[offset + i + 1]` for `i` in `[0, len)`, with no check that `offset + i + 1 < words.length`: [1](#0-0) 

`extractBytesArray` performs a single guard (`offset > words.length - 1`) before the loop, but that guard only protects the *first* access to `words[offset]`. Inside the loop it computes a second attacker-controlled `bytesOffset` from `words[offset + i + 1]` and a third index `words[offset + bytesOffset + 1]`, neither of which is checked against `words.length` before being dereferenced: [2](#0-1) 

Both `len` and `bytesOffset` are fully attacker-controlled 32-byte words taken from the calldata passed to a precompiled contract (e.g. `BatchValidateSign`/`ValidateMultiSign`, which are registered in the same file's precompile table). By supplying a small `words` array (short calldata) together with a large `len`/`bytesOffset` value, the loop index walks past the end of the backing `DataWord[]`, exactly the same "trusted partition length not validated against actual array size" bug class described in the TensorFlow advisory (`RaggedCountSparseOutput` trusting `splits` to properly partition `values`).

### Impact Explanation
An out-of-bounds `words[...]` access throws an uncaught `ArrayIndexOutOfBoundsException` while executing inside the TVM during transaction/contract-call processing. Any account that can broadcast a transaction invoking a smart contract that calls the affected precompiled contract address can trigger this path — no special privilege is required. Depending on whether this exception is caught by an outer handler in the precompiled-contract dispatch path in `Program`, this results at minimum in an unrecoverable/incorrectly-handled execution failure for that call, and at worst in an exception escaping the transaction-execution sandbox during block application, disrupting the node's ability to process/validate the block. I was unable to fully trace, within available tool calls, whether every call site wraps these helpers in a blanket `try/catch(Throwable)` (as is done for `recoverAddrBySign` in the same file), so the exact severity (revert-only vs. broader execution/node-level fault) could not be conclusively confirmed and should be verified directly.

### Likelihood Explanation
High reachability: the trigger is a single transaction that calls a contract invoking the vulnerable precompiled contract with malformed calldata (an out-of-range length/offset word) — no elevated privilege, staking, or coordination with other nodes is needed.

### Recommendation
Add explicit bounds checks in `extractBytes32Array` and `extractBytesArray` before every indexed access into `words` (i.e., validate `offset + i + 1 < words.length` for every loop iteration and validate `bytesOffset`/`offset + bytesOffset + 1` against `words.length` before dereferencing), returning an empty array or throwing a controlled/catchable exception (e.g. `RuntimeException` that is converted to a normal VM revert) instead of allowing a raw `ArrayIndexOutOfBoundsException` to propagate.

### Proof of Concept
1. Deploy a contract that calls the precompiled contract address backed by `extractBytes32Array`/`extractBytesArray` (e.g. `BatchValidateSign`), supplying calldata whose declared array-length word (or nested offset word) is much larger than the actual number of 32-byte words present in the calldata.
2. Broadcast a transaction invoking this call from any unprivileged account.
3. During TVM execution, `words[offset + i + 1]` (or the nested `bytesOffset`/`bytesLen` accesses) indexes past the end of the `words` array, throwing `ArrayIndexOutOfBoundsException`; observe whether this exception propagates beyond the intended per-call revert boundary. [3](#0-2)

### Citations

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L390-412)
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
```
