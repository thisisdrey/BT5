### Title
Unbounded attacker-controlled array-length in TVM precompile ABI decoding causes OutOfMemoryError / node crash - (File: `actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java`)

### Summary
The MariaDB CVE crashes the server because a network-supplied packet size is passed straight into `alloca()` without any upper bound, letting an unauthenticated client trigger unbounded stack allocation. `PrecompiledContracts.java`'s helper functions used by the `BatchValidateSign` / `ValidateMultiSign` TVM precompiles (addresses `0x9` / `0xa`, reachable via `CALL` from any smart contract) show the same pattern: an attacker-controlled length word is read straight from call data and used to size a Java array with no bound check against the actual data length or any sane maximum.

### Finding Description
`extractBytesArray`, `extractBytes32Array`, and `extractSigArray` decode ABI-encoded arrays from the raw call data handed to the `BatchValidateSign`/`ValidateMultiSign` precompiled contracts: [1](#0-0) [2](#0-1) [3](#0-2) 

In each case, `len` is obtained from a single `DataWord` in the caller-supplied data via `intValueSafe()`, then used directly as the dimension of a new array (`new byte[len][]`) with no comparison against `words.length`, the actual call-data size, or any protocol-defined maximum. `intValueSafe()` clamps to `Integer.MAX_VALUE` rather than rejecting the value, so a crafted 32-byte word (e.g. `0xFFFFFFFF...`) is accepted as a "valid" length and an allocation of up to `Integer.MAX_VALUE` object-array slots is attempted immediately — this is functionally the same bug class as the CVE: an unauthenticated caller supplies a size field that flows unchecked into a bulk memory allocation.

This is reachable from a signed transaction that invokes any smart contract executing a `CALL` opcode to precompile address `0x0000...09` (`BatchValidateSign`) or `0x0000...0a` (`ValidateMultiSign`), gated only by `VMConfig.allowTvmSolidity059()` (a mainnet-enabled feature flag), as registered in `getContractForAddress`: [4](#0-3) 

### Impact Explanation
Allocating a Java array sized near `Integer.MAX_VALUE` throws `OutOfMemoryError`, which is an `Error`, not a checked/unchecked `Exception`; depending on how deep in the executor call stack it originates and how it is caught, it can destabilize the JVM heap for concurrently executing threads (other block/transaction processing), leading to node crash or forced halt. Since this is triggerable by any unprivileged account able to deploy and call a contract (no special permission or stake required), it is a low-cost, repeatable denial-of-service vector against any full node that executes the transaction, matching the CVE's "large packet can crash the server" impact class.

### Likelihood Explanation
Likelihood is high given a single crafted transaction calling a contract that issues one `CALL` to precompile `0x9`/`0xa` with a manipulated length word is sufficient; no elevated privileges, large stake, or special network position are required, only that `allowTvmSolidity059` is enabled (a standard chain parameter on mainnet-class configurations).

### Recommendation
Bound-check `len` in `extractBytesArray`, `extractBytes32Array`, and `extractSigArray` against the actual remaining call-data length (or a fixed sane maximum, e.g., the maximum number of signatures/addresses the precompile logically supports) before allocating the array, rejecting the call with an execution failure instead of attempting the allocation.

### Proof of Concept
Deploy a trivial contract that performs a low-level `CALL` (or use `abi.encodePacked`/inline assembly) to address `0x0000000000000000000000000000000000000009` (or `0xa`) with call data whose header word encoding the array length (`words[offset]`) is set to `0xFFFFFFFF...` (max `DataWord`). Broadcasting one such transaction drives execution into `extractBytesArray`/`extractSigArray`, which immediately attempts `new byte[0xFFFFFFFF][]`, triggering `OutOfMemoryError` inside `BatchValidateSign.execute`/`ValidateMultiSign.execute`. [1](#0-0)

### Citations

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L254-259)
```java
    if (VMConfig.allowTvmSolidity059() && address.equals(batchValidateSignAddr)) {
      return batchValidateSign;
    }
    if (VMConfig.allowTvmSolidity059() && address.equals(validateMultiSignAddr)) {
      return validateMultiSign;
    }
```

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
