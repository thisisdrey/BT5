### Title
Unvalidated attacker-controlled array-length allocation in `ValidateMultiSign` precompile causes unbounded memory allocation / DoS - (File: `actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java`)

### Summary
The `ValidateMultiSign` precompiled contract (invoked via TVM `CALL`/`STATICCALL` to its precompile address) decodes a signature-count field taken directly from calldata and uses it to allocate Java arrays **before** any bound is enforced, mirroring the CVE-2017-12145 pattern where `quicktime_read_ftyp` allocates memory from an unvalidated size field taken from untrusted input, causing an allocation failure / DoS.

### Finding Description
`ValidateMultiSign.execute()` reads the signature array length from a `DataWord` inside the raw calldata and passes it straight into helper functions that allocate arrays sized by that value: [1](#0-0) 

The `sigArraySize > MAX_SIZE` guard is only executed when `VMConfig.allowTvmSelfdestructRestriction()` is enabled; when that flag is off, `extractBytesArray` is called directly with the attacker-controlled `len` with no upper bound check performed beforehand. Inside the extraction helpers, the length taken from calldata (`words[offset].intValueSafe()`) is used to allocate a two-dimensional array immediately: [2](#0-1) [3](#0-2) 

The `signatures.length == 0 || signatures.length > MAX_SIZE` check (line 1076) only runs *after* the array has already been allocated, so it cannot prevent the allocation attempt itself. In addition, `extractBytesArray` reads a second attacker-controlled length, `bytesLen`, for every array element and passes it to `extractBytes`, which performs `Arrays.copyOfRange(data, offset, offset + len)` — another allocation sized purely by attacker-supplied data, independent of the `MAX_SIZE` guard entirely.

Energy metering for this precompile is computed only from the calldata length (`getEnergyForData`), not from the decoded length fields: [4](#0-3) 

so an attacker can craft a small calldata payload that encodes a very large `len`/`bytesLen` value, paying minimal energy while forcing the JVM to attempt a huge allocation.

### Impact Explanation
A crafted `len` (or `bytesLen`) value close to `Integer.MAX_VALUE` forces `new byte[len][]` or `Arrays.copyOfRange` to attempt allocating gigabytes of heap. Although java-tron wraps VM execution in a broad `catch (Throwable e)` in `VMActuator.execute` (which would also catch a resulting `OutOfMemoryError`) and a dedicated `OutOfMemoryException`/`checkMemorySize` mechanism exists for the EVM `Memory` object, that protection only covers TVM opcode memory growth — it does **not** cover ad-hoc array allocations performed inside `PrecompiledContracts` helper methods, which bypass `Memory.extend()`/`MEM_LIMIT` entirely. A real `java.lang.OutOfMemoryError` triggered here can exhaust heap for the whole JVM process (not just the single transaction thread), risking node instability or crash beyond the executing transaction — consistent with the CVE class of "allocation failure causing denial of service" via an unvalidated size field.

### Likelihood Explanation
The precompile is reachable by any unprivileged account via a plain smart-contract `CALL`/`STATICCALL` to the `ValidateMultiSign` precompile address with hand-crafted calldata — no special privilege, deployment, or SR/witness role is required. Exploitability depends on the `intValueSafe()` bound (it likely caps to `Integer.MAX_VALUE` given the pattern used elsewhere) and current default of `allowTvmSelfdestructRestriction()`; if that config is disabled (or for element-level `bytesLen`, unconditionally), the guard offers no protection.

### Recommendation
Validate all length fields (`sigArraySize`, `len`, `bytesOffset`, `bytesLen`) extracted from calldata against a strict maximum (e.g., `MAX_SIZE`, and against `rawData.length` bounds) *before* performing any array allocation or `Arrays.copyOfRange` call in `extractBytes32Array`, `extractBytesArray`, `extractSigArray`, and `extractBytes`. Move the `sigArraySize > MAX_SIZE` check so it is unconditionally enforced prior to calling `extractBytesArray`/`extractSigArray`, regardless of `allowTvmSelfdestructRestriction()`.

### Proof of Concept
1. Deploy or use an existing contract that performs a `STATICCALL`/`CALL` to the `ValidateMultiSign` precompile address.
2. Construct calldata following the ABI layout expected by `ValidateMultiSign.execute()` where the length word at `words[3].intValueSafe() / WORD_SIZE]` (the signature-array length) is set to a very large value (e.g. `0xFFFFFFFF`), while keeping the outer calldata short so `getEnergyForData` reports low energy cost.
3. Send the transaction with `allowTvmSelfdestructRestriction()` disabled (or target the always-unchecked `bytesLen` field consumed by `extractBytesArray`).
4. Observe the node attempt `new byte[len][]` for the crafted `len`, producing an `OutOfMemoryError` inside precompile execution that is not covered by the `Memory`/`checkMemorySize` energy-based protections used elsewhere in the TVM.

### Citations

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

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1044-1049)
```java
    @Override
    public long getEnergyForData(byte[] data) {
      long cnt = (data.length / WORD_SIZE - 5) / 5;
      // one sign 1500, half of ecrecover
      return cnt * ENGERYPERSIGN;
    }
```

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1066-1074)
```java
      if (VMConfig.allowTvmSelfdestructRestriction()) {
        int sigArraySize = words[words[3].intValueSafe() / WORD_SIZE].intValueSafe();
        if (sigArraySize > MAX_SIZE) {
          return Pair.of(true, DATA_FALSE);
        }
      }
      byte[][] signatures = VMConfig.allowTvmSelfdestructRestriction() ?
          extractSigArray(words, words[3].intValueSafe() / WORD_SIZE, rawData) :
          extractBytesArray(words, words[3].intValueSafe() / WORD_SIZE, rawData);
```
