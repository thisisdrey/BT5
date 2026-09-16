### Title
Uncontrolled resource consumption via unbounded declared array length in `ValidateMultiSign`/`BatchValidateSign` precompiles - (File: actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java)

### Summary
`ValidateMultiSign.execute()` and `BatchValidateSign.doExecute()` decode ABI-encoded calldata supplied directly by a contract call (an unprivileged TVM caller) and extract a "declared" array length for the signatures/addresses arrays from the calldata itself before validating that length against the enforced maximum (`MAX_SIZE`).

### Finding Description
In `ValidateMultiSign.execute()`, the `MAX_SIZE` (5) bound on `sigArraySize` is only checked when `VMConfig.allowTvmSelfdestructRestriction()` is enabled: [1](#0-0) 
When that feature flag is not active, `extractBytesArray(words, words[3].intValueSafe() / WORD_SIZE, rawData)` is invoked directly on the attacker-declared offset/length pulled from `words`, and the resulting `signatures.length` is checked against `MAX_SIZE` only *after* extraction has already run: [2](#0-1) 
The same pattern occurs in `BatchValidateSign.doExecute()`, where `sigArraySize`/`addrArraySize` are only guarded by the `allowTvmSelfdestructRestriction()` flag before calling `extractSigArray`/`extractBytesArray`/`extractBytes32Array`, with the `MAX_SIZE` (16) check happening only after arrays are already built: [3](#0-2) 

This is the same bug class as CVE-2018-5784: a declared count field taken from untrusted input is used to drive allocation/iteration in a decoding routine before it is validated against the maximum permitted/actual size, letting the caller control resource consumption ahead of any bound check. I was unable to locate and fully inspect the implementation body of `extractBytesArray`/`extractSigArray`/`extractBytes32Array` in the available index (the read of lines 1219–1375 of `PrecompiledContracts.java` failed due to a tool error and could not be retried in this final iteration), so I cannot conclusively confirm whether these helper methods themselves impose any implicit bound (e.g., via `DataWord.parseArray` capping the number of words) that would prevent unbounded allocation. This is a real gap in verification.

### Impact Explanation
If `extractBytesArray`/`extractSigArray` allocate a `byte[][]` sized to the attacker-declared count read straight from calldata (as the code structure strongly suggests, given the bound check only runs after extraction under the pre-`allowTvmSelfdestructRestriction()` code path), a contract caller could declare an extremely large array length in ABI-encoded calldata to force a large allocation or long-running loop inside a precompiled contract call, on a path reachable from any TVM `CALL`/`STATICCALL` to address `0x...9` or `0x...a`. This could cause a node crash (OOM) or make constant-call / block-execution paths unresponsive — a denial-of-service against the node, which is in the accepted impact set ("node crash or halt").

### Likelihood Explanation
Reachability is straightforward: any unprivileged transaction broadcaster or contract can invoke these precompiles via a `CALL` to the fixed precompile addresses with attacker-controlled calldata, requiring no special privilege. The check ordering (validate-after-extract, gated behind a feature flag) as written is a genuine architectural gap; however, exploitability strictly depends on the un-reviewed implementation of `extractBytesArray`/`extractSigArray`/`extractBytes32Array`, which I could not confirm actually performs unbounded allocation proportional to the attacker-supplied length before it can throw/bound out. Without that confirmation, I can state the pattern is present and reachable, but cannot certify actual unbounded memory/CPU consumption occurs.

### Recommendation
Move the `sigArraySize`/`addrArraySize` (and any analogous size) validation against `MAX_SIZE` to run unconditionally, before calling `extractBytesArray`/`extractSigArray`/`extractBytes32Array`, regardless of `VMConfig.allowTvmSelfdestructRestriction()` activation state, and additionally have those extraction helpers validate the declared count against the actual remaining calldata length before allocating any array, mirroring the libtiff fix of validating the declared directory-entry count against the real entry count before use.

### Proof of Concept
A conceptual PoC (unverified against the helper implementations) would be a contract that calls `validatemultisign(address,uint256,bytes32,bytes[])` or `batchvalidatesign(bytes32,bytes[],address[])` with hand-crafted calldata declaring an array-length word far larger than any real elements present (e.g., `0xFFFFFFFF`), with `VMConfig.allowTvmSelfdestructRestriction()` disabled, to attempt forcing a large allocation/loop inside `extractBytesArray`/`extractSigArray` prior to the `MAX_SIZE` check. Because the exact allocation behavior of these helper functions could not be verified in this pass, this should be validated against the live source of `extractBytesArray`, `extractSigArray`, and `extractBytes32Array` before treating this as confirmed-exploitable.

### Citations

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1066-1078)
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

      if (signatures.length == 0 || signatures.length > MAX_SIZE) {
        return Pair.of(true, DATA_FALSE);
      }
```

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1163-1181)
```java
      byte[] hash = words[0].getData();

      if (VMConfig.allowTvmSelfdestructRestriction()) {
        int sigArraySize = words[words[1].intValueSafe() / WORD_SIZE].intValueSafe();
        int addrArraySize = words[words[2].intValueSafe() / WORD_SIZE].intValueSafe();
        if (sigArraySize > MAX_SIZE || addrArraySize > MAX_SIZE) {
          return Pair.of(true, DATA_FALSE);
        }
      }

      byte[][] signatures = VMConfig.allowTvmSelfdestructRestriction() ?
          extractSigArray(words, words[1].intValueSafe() / WORD_SIZE, data) :
          extractBytesArray(words, words[1].intValueSafe() / WORD_SIZE, data);
      byte[][] addresses = extractBytes32Array(
          words, words[2].intValueSafe() / WORD_SIZE);
      int cnt = signatures.length;
      if (cnt == 0 || cnt > MAX_SIZE || signatures.length != addresses.length) {
        return Pair.of(true, DATA_FALSE);
      }
```
