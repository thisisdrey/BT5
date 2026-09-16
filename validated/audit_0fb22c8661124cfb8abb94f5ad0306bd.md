Based on the investigation, the closest reachable analog to CVE-2021-32286's unchecked option-length walk in `pcapngoptionwalk` is in java-tron's TVM precompiled-contract argument extraction helpers, which walk through a length-prefixed ABI array without validating each element against the actual `words` array bounds.

### Title
Unchecked ABI array length in precompiled contract input parsing leads to out-of-bounds array access - (File: actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java)

### Summary
`extractBytesArray` and `extractSigArray` in `PrecompiledContracts.java` decode a variable-length array of byte strings / signatures from precompile call data by reading a caller-controlled element count (`len`) and then looping `i` from `0` to `len` reading `words[offset + i + 1]` without ever re-checking that `offset + i + 1` stays within `words.length` for each iteration [1](#0-0) . Only the initial `offset` is checked against `words.length - 1`; the count `len` itself is taken directly from calldata via `words[offset].intValueSafe()` with no upper bound tied to the actual number of remaining words [2](#0-1) .

### Finding Description
This mirrors the CVE's root cause class: a hand-rolled "walk N length-prefixed items" loop that trusts an attacker-supplied count field instead of bounding it by the actual buffer size, exactly like `pcapngoptionwalk` trusting an option-length field while walking a pcapng buffer. Here, `len` comes straight from the calldata word at `offset` and is never validated against `words.length`, so a transaction/contract call can supply a `len` far larger than the number of words actually present in the ABI-encoded input, causing the loop to index past the end of the `words` array.

### Impact Explanation
An out-of-bounds `words[...]` access throws an uncaught `ArrayIndexOutOfBoundsException` during TVM precompiled-contract execution. If this exception is not caught by the surrounding VM/Program execution wrapper, it can abort transaction/block processing unexpectedly, which is a node-crash/halt class impact reachable purely via a contract call with crafted calldata (no special privileges required) — fitting the "unprivileged contract deployer/caller reaches TVM precompiles" category described in scope.

### Likelihood Explanation
Reaching this code only requires invoking the precompiled contract that uses `extractBytesArray`/`extractSigArray` (e.g. the batch signature validation precompile) with a specially crafted ABI-encoded array-length field, something any account can do by sending a transaction that calls the precompile address directly or via a contract. No special TVM opcode sequence beyond a normal external call is needed.

### Recommendation
Bound `len` by the number of words actually available (`words.length - offset - 1`) before entering the loop in `extractBytesArray` and `extractSigArray`, and validate each computed index (`offset + i + 1`, and `bytesOffset + offset + 2` plus `bytesLen`) against `words.length`/`data.length` before use, returning an execution failure instead of throwing an unchecked exception when the encoding is malformed.

### Proof of Concept
Craft calldata for the precompile using these helpers where the length word at `offset` (e.g. `words[offset]`) encodes a very large count (e.g. `0xFFFFFFFF` truncated via `intValueSafe()`), while the calldata is otherwise short/truncated so that `words.length` is small. Calling the precompile with this input drives the loop in `extractBytesArray`/`extractSigArray` to read `words[offset + i + 1]` beyond the array bounds, throwing `ArrayIndexOutOfBoundsException` at [3](#0-2) .

**Caveat**: I could not confirm within the indexed code whether the TVM's precompile invocation path (e.g. in `Program`/`VM` opcode dispatch) wraps `execute()` calls in a catch-all that safely reverts on `RuntimeException`, which would downgrade this from a node-crash to a contained transaction failure. This should be verified in a full checkout, since the actual severity hinges on whether the exception propagates uncaught during block application.

### Citations

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L399-426)
```java
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
