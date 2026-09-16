### Title
Unbounded array-length field in TVM precompiled-contract ABI decoding causes uncaught OutOfMemoryError/ArrayIndexOutOfBoundsException - (File: `actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java`)

### Summary
`PrecompiledContracts.java` implements the ad-hoc ABI decoders used by the `ValidateMultiSign` and `BatchValidateSign` precompiled contracts (reachable via a plain `CALL`/`STATICCALL` from any deployed smart contract). These decoders read an attacker-controlled 32-byte "array length" word straight out of the calldata and use it directly to size a Java array and to index into the `words[]` array, with no upper-bound check against the actual size of the decoded calldata — the same bug class as the CVE's `CiffDirectory::readDirectory` integer-overflow/OOB read triggered by a crafted length field in untrusted input.

### Finding Description
`extractBytes32Array`, `extractBytesArray`, and `extractSigArray` all take an `offset` into the ABI-decoded `words[]` array and read the length as:
```java
int len = words[offset].intValueSafe();
byte[][] bytesArray = new byte[len][];
for (int i = 0; i < len; i++) {
  ...
  bytesArray[i] = extractBytes(data, (bytesOffset + offset + 2) * WORD_SIZE, bytesLen);
}
``` [1](#0-0) 

`len` is fully attacker-controlled (it comes from a `DataWord` derived from the calldata of the contract call). `intValueSafe()` clamps to `Integer.MAX_VALUE`/negative bounds but does **not** validate that `len` is consistent with `words.length` or with the actual size of `data`. Consequently:
- A large `len` (e.g., close to `Integer.MAX_VALUE`) causes `new byte[len][]` to throw `OutOfMemoryError`, which is an `Error`, not an `Exception` — a class of throwable typically not caught by generic `catch (Exception e)` handlers used elsewhere in the VM/precompiled-contract dispatch path.
- A `len` value that is smaller but still larger than the real number of remaining words causes `words[offset + i + 1]` to walk past the end of the `words` array, throwing `ArrayIndexOutOfBoundsException`.
- `extractBytes` itself performs `Arrays.copyOfRange(data, offset, offset + len)` with attacker-controlled `offset`/`len` derived the same way, which can throw further unchecked exceptions if those indices exceed `data.length`. [2](#0-1) 

This mirrors the CVE-2019-13110 pattern: a length/offset field taken from untrusted, structured input is used to size/index memory without validating it against the real bounds of the buffer, producing an out-of-bounds access or an integer/allocation blow-up purely from crafted input.

### Impact Explanation
These decoding helpers back the `ValidateMultiSignContract` and `BatchValidateSignContract` precompiled contracts, which are exposed to any account or smart contract that can issue a `CALL`/`STATICCALL` opcode against the corresponding precompile address — i.e., reachable by an unprivileged contract deployer/caller with no special permissions. If the resulting `OutOfMemoryError` or unhandled `ArrayIndexOutOfBoundsException` propagates past the transaction-execution try/catch boundaries in the TVM interpreter (`Program`/`VM` classes), it can abort or destabilize the node process handling that transaction, denying service (crash) to that node, and — because all full nodes execute every transaction identically — potentially to every full node on the network that processes the malicious transaction. This matches the "node crash or halt" impact bar in the validation rules.

### Likelihood Explanation
Likelihood is high for reaching the vulnerable code: constructing calldata with an oversized length word for `ValidateMultiSignContract`/`BatchValidateSignContract` requires no special privileges — any address can broadcast a contract-call transaction invoking these precompiles with hand-crafted ABI data. The remaining uncertainty is whether the outer TVM dispatch code (`Program`'s precompiled-contract invocation path) catches broad `Throwable`/`Error` around precompiled-contract execution; I could not fully confirm this within the available investigation, since the call sites and exception-handling wrappers around `PrecompiledContract.execute()` were not fully traced in this session. If such a broad catch exists, the practical impact is downgraded to a reverted transaction (denial of service on this contract only) rather than a full node crash.

### Recommendation
- In `extractBytes32Array`, `extractBytesArray`, `extractSigArray`, and `extractBytes`, validate `len`/`offset` against `words.length` and `data.length` before allocating or indexing, rejecting (returning failure / reverting) instead of throwing unchecked exceptions.
- Ensure the precompiled-contract execution path wraps calls in a handler that treats decoding failures (including `OutOfMemoryError`/`ArrayIndexOutOfBoundsException`) as a normal contract-execution failure (revert) rather than allowing them to propagate and destabilize the node.

### Proof of Concept
1. Deploy a trivial contract that performs a `STATICCALL` to the `ValidateMultiSignContract`/`BatchValidateSignContract` precompile address with calldata whose ABI "length" word (at the offset consumed by `extractBytesArray`/`extractBytes32Array`) is set to a very large value (e.g., `0x7FFFFFFF`), while keeping the rest of the calldata short.
2. Broadcast this contract call as an ordinary transaction.
3. During execution, `extractBytesArray`/`extractBytes32Array` compute `len` from the crafted word and execute `new byte[len][]`, triggering an `OutOfMemoryError`, or the subsequent loop accesses `words[offset + i + 1]` beyond the actual array bounds, triggering an `ArrayIndexOutOfBoundsException`.
4. Observe whether this throwable propagates out of the transaction-processing call stack; if uncaught by a `Throwable`-level guard, the executing full node's JVM can crash or become unresponsive while processing this single transaction.

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

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L428-430)
```java
  private static byte[] extractBytes(byte[] data, int offset, int len) {
    return Arrays.copyOfRange(data, offset, offset + len);
  }
```
