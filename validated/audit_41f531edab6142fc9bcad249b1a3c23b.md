### Title
Precompiled contract ABI array decoding allocates arrays from unvalidated attacker-controlled length words, causing OutOfMemoryError DoS - (File: actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java)

### Summary
The MP4v2 CVE root cause is an "Entry Number" field read from untrusted input and used directly to size a memory allocation/loop bound without validating it against the actual available data, leading to a crash. The same pattern exists in `PrecompiledContracts.extractBytes32Array`, `extractBytesArray`, and `extractSigArray`, which decode ABI-style dynamic arrays passed as `data` to certain TVM precompiled contracts.

### Finding Description
These helper methods read a length/count word directly from the `DataWord[] words` array (which is simply the raw call `data` sliced into 32-byte words) and immediately use it to allocate an array, with no upper bound check against the actual remaining `data` length before allocation: [1](#0-0) [2](#0-1) [3](#0-2) 

In each case `len` is taken via `words[offset].intValueSafe()` from a caller-controlled 256-bit word, then used as `new byte[len][]`, exactly mirroring the MP4Atom bug class: a table "entry count" field is trusted and used for allocation before validating it fits the surrounding buffer. A caller can craft a transaction/contract call whose `data` sets this length word to a very large value (e.g., near `Integer.MAX_VALUE`, since `intValueSafe()` only clamps to `int` range, it does not clamp to the actual buffer size), causing an immediate large-object allocation attempt (`new byte[len][]`) on the node executing the transaction, before the subsequent loop would fail with an `ArrayIndexOutOfBoundsException` on out-of-range access.

A similar unclamped-count allocation pattern also occurs in the Sapling proof-verification precompile, where `spendCount`/`receiveCount` derived from call data directly size multiple `byte[][]` arrays before further use: [4](#0-3) .

Note: I was unable to fully trace, in this session, the exact `execute()` entry point(s) that call `extractBytes32Array`/`extractBytesArray`/`extractSigArray` (index/tool limitations prevented resolving the precise caller and the exact upstream constraint on `len`, if any) or the exact code that computes `spendCount`/`receiveCount` in the Sapling precompile. This weakens certainty about whether an outer bound check already exists elsewhere in the calling precompile before these helpers run.

### Impact Explanation
If reachable with an unvalidated length, a single crafted contract call (via `CALL`/`STATICCALL` to the relevant precompiled contract address) could force the node's JVM to attempt allocating an oversized array, throwing `OutOfMemoryError`. Because `OutOfMemoryError` is unchecked and can occur outside normal exception-handling paths in transaction execution, this can crash or destabilize the block-producing/full node process, denying transaction/API service — matching the CVE's "denial of service (overflow, insufficient memory allocation)" impact class.

### Likelihood Explanation
Exploitability is uncertain without confirming (a) which precompiled contract(s) actually invoke these helpers on attacker-supplied `data`, and (b) whether any outer bounds check (e.g., comparing `len` to `data.length / 32`) already exists before invocation. Given I could not locate such a check in the visible portions of the file, and `intValueSafe()` is documented elsewhere in the codebase as merely clamping to `int` range (not to buffer size), likelihood is plausible but not fully confirmed.

### Recommendation
Add an explicit bound check in `extractBytes32Array`, `extractBytesArray`, and `extractSigArray` (and in the Sapling `spendCount`/`receiveCount` handling) that rejects `len` values inconsistent with the actual size of `words`/`data` before allocating any array, returning an execution failure instead of attempting allocation.

### Proof of Concept
Not fully constructible without confirming the exact precompile address/ABI that reaches `extractBytes32Array`/`extractBytesArray`/`extractSigArray` with attacker-controlled offsets — this could not be verified within the available tool budget. Conceptually: craft `data` for the relevant precompile such that the 32-byte word at the expected "array length" offset encodes a very large integer (e.g., `0x7fffffff`), then invoke the precompile via a contract call; if no bound check precedes the array allocation, the node attempts `new byte[len][]` and may throw `OutOfMemoryError`.

### Citations

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L390-397)
```java
  private static byte[][] extractBytes32Array(DataWord[] words, int offset) {
    int len = words[offset].intValueSafe();
    byte[][] bytes32Array = new byte[len][];
    for (int i = 0; i < len; i++) {
      bytes32Array[i] = words[offset + i + 1].getData();
    }
    return bytes32Array;
  }
```

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L399-412)
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
```

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L414-426)
```java
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

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1502-1509)
```java
        byte[][] spendCv = new byte[spendCount][32];
        byte[][] rk = new byte[spendCount][32];
        byte[][] spendProof = new byte[spendCount][192];
        byte[][] spendAuthSig = new byte[spendCount][64];
        byte[][] receiveCm = new byte[receiveCount][32];
        byte[][] receiveCv = new byte[receiveCount][32];
        byte[][] receiveEpk = new byte[receiveCount][32];
        byte[][] receiveProof = new byte[receiveCount][192];
```
