### Title
Unvalidated ABI array-length fields in TVM precompiled-contract array extraction enable out-of-bounds access / unbounded allocation - ([File: actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java])

### Summary
The CVE describes a buffer overflow in an MMS client caused by trusting an attacker-supplied length field (`FileDirResponse`) to size/copy a fixed buffer without validating it against the actual available data. The closest reachable analog in java-tron is in the ABI-array decoding helpers used by TVM precompiled contracts, where a length word taken directly from calldata is used both to allocate an array and to index into the `words` array, with no check that the declared length is consistent with the size of the input.

### Finding Description
`extractBytesArray`, `extractSigArray`, and `extractBytes32Array` in `PrecompiledContracts.java` read a length value from attacker-controlled calldata (`words[offset].intValueSafe()`) and then use it directly to allocate a Java array and iterate: [1](#0-0) 

- `len` comes straight from the caller-supplied `DataWord`, with `intValueSafe()` only guarding against overflow-into-negative-long, not against the value being wildly larger than the actual number of elements in `words`.
- The loop body then reads `words[offset + i + 1]` for `i` up to `len - 1` with no check that `offset + i + 1 < words.length`, so a crafted `len` causes `ArrayIndexOutOfBoundsException`.
- `new byte[len][]` (and, transitively, `extractBytes`'s `Arrays.copyOfRange(data, offset, offset + len)`) allocate based entirely on attacker-controlled length, which can also trigger `NegativeArraySizeException` or attempt to allocate a huge array (`OutOfMemoryError`) if `len`/`bytesLen` is chosen to be very large.

This mirrors the CVE's root cause exactly: a length field taken from untrusted input is used to size/index a buffer without validating it against the bounds of the actual backing data, only here the buffer is a Java array (`words`/`data`) reachable from calldata submitted with an ordinary smart-contract call.

`extractBytesArray`/`extractSigArray` are the ABI-decoding helpers for the multi-signature-validation family of precompiled contracts, which are reachable by any account that deploys or calls a contract invoking those precompiled addresses (e.g. via `CALL`/`STATICCALL` from Solidity). This is executed inside `Program.callToPrecompiledAddress`, which invokes `contract.execute(data)` directly: [2](#0-1) 

### Impact Explanation
- At minimum, a crafted length field lets any unprivileged contract caller throw an uncaught `ArrayIndexOutOfBoundsException`/`NegativeArraySizeException` deep inside precompiled-contract execution triggered from ordinary TVM `CALL`s, an unusual code path compared to normal ABI/EVM decoding elsewhere in the codebase (which validates lengths against buffer size, e.g. `RLP.verifyLength`, `ContractEventParser.subBytes`).
- If `len`/`bytesLen` is chosen large enough, `new byte[len][]` or `Arrays.copyOfRange` can attempt a very large allocation, risking `OutOfMemoryError`. Whether this propagates to crash node processing (rather than being caught as a normal execution exception and reverting the transaction) depends on exception-handling layers above `Program.callToPrecompiledAddress`/`Runtime`, which I was not able to fully trace in the time available — this is the main remaining uncertainty in this analog.
- Other precompiled contracts in the same file that copy fixed-size chunks (e.g. `VerifyBurnProof`, `MerkleHash`) explicitly wrap their logic in `try { } catch (Throwable any)`, showing the codebase is aware such crafted inputs can throw, but `extractBytesArray`/`extractSigArray`/`extractBytes32Array` themselves have no such guard and no length validation against `words.length`/`data.length` before use.

### Likelihood Explanation
Any account can broadcast a transaction that deploys or triggers a contract making a `CALL`/`STATICCALL` to a precompiled address whose implementation decodes its input with `extractBytesArray`/`extractSigArray`/`extractBytes32Array`, supplying an arbitrarily large or negative-looking length word in the calldata. No special privileges, keys, or peer/witness position are required — this matches "unprivileged transaction broadcaster / contract deployer" per the analog scope.

### Recommendation
Validate the decoded `len` (and derived `bytesOffset`/`bytesLen`) in `extractBytesArray`, `extractSigArray`, and `extractBytes32Array` against `words.length` and `data.length` before allocating arrays or indexing, mirroring the bounds checks already used in `RLP.verifyLength` and `ContractEventParser.subBytes`. Reject (return empty/throw a caught, well-typed exception) rather than allocate/index when the declared length is inconsistent with the available data, and confirm the precompiled-contract execution path in `Program`/`Runtime` catches `Throwable` (not just `Exception`) so any residual `Error` cannot escape to crash block processing.

### Proof of Concept
1. Deploy or use an existing contract that performs a `CALL`/`STATICCALL` into a precompiled address that decodes its calldata with `extractBytesArray`/`extractSigArray` (the multi-signature-validation precompile family).
2. Craft the calldata so the ABI "array length" word at the expected offset (`words[offset]`) is set to a very large value (e.g. `0x7fffffff`) or a value that, combined with `offset`, pushes indices past `words.length`.
3. Submit the triggering transaction; observe that `extractBytesArray`/`extractSigArray` throws `ArrayIndexOutOfBoundsException`/`NegativeArraySizeException`, or attempts a huge allocation, when executed via `Program.callToPrecompiledAddress` → `contract.execute(data)`. [3](#0-2) [2](#0-1)

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

**File:** actuator/src/main/java/org/tron/core/vm/program/Program.java (L1748-1753)
```java
      contract.setRepository(deposit);
      contract.setResult(this.result);
      contract.setConstantCall(isConstantCall());
      contract.setVmShouldEndInUs(getVmShouldEndInUs());
      Pair<Boolean, byte[]> out = contract.execute(data);

```
