### Title
Unbounded array allocation in `ValidateMultiSign` precompile's legacy signature-array decode path can OOM/DoS the node - (File: actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java)

### Summary
`PrecompiledContracts.ValidateMultiSign.execute()` decodes a caller-supplied "signatures" array from contract calldata. When the `allowTvmSelfdestructRestriction` hard-fork flag is *not* active, the code calls the legacy `extractBytesArray(words, offset, rawData)` helper instead of the bounds-checked `extractSigArray`, and does so **before** the `signatures.length > MAX_SIZE` check runs. `extractBytesArray` reads an attacker-controlled 32-byte length word and immediately does `new byte[len][]`, i.e. a large loop/allocation driven directly by untrusted length data — the same bug class as CVE-2018-7173's `JBIG2Stream::readSymbolDictSeg`, which reads a length field from attacker data and drives a large, unchecked loop/allocation.

### Finding Description [1](#0-0) 

```java
public Pair<Boolean, byte[]> execute(byte[] rawData) {
  ...
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
  ...
```

The bound check (`MAX_SIZE = 5`) on the decoded array size is only performed *before* extraction when `allowTvmSelfdestructRestriction()` is true. If that flag is false, the size limit is never enforced before allocation — `extractBytesArray` is invoked directly: [2](#0-1) 

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

`len` is a fully attacker-controlled 32-byte word read from calldata via `intValueSafe()`, with no upper bound applied before `new byte[len][]`. A value close to `Integer.MAX_VALUE` triggers an immediate large-memory allocation attempt (an `OutOfMemoryError`), or, if allocation succeeds, the subsequent loop drives array-index accesses on `words` well past its real length. Unlike `BatchValidateSign.execute()`, which wraps its whole `doExecute()` call in a `catch (Throwable t)`, `ValidateMultiSign.execute()` has **no surrounding try/catch** around this extraction call — only the later weight-computation block is wrapped in `try { ... } catch (Throwable t)`. An `OutOfMemoryError`/`ArrayIndexOutOfBoundsException` thrown during `extractBytesArray` therefore propagates unguarded out of the precompile.

### Impact Explanation
Any account can deploy a trivial contract that calls the `validatemultisign(address,uint256,bytes32,bytes[])` precompile address with crafted calldata encoding a huge `bytes[]` length word. This is directly reachable from a single signed `TriggerSmartContract` transaction — no special privileges needed — matching the "contract deployer / caller reaches TVM precompiles" in-scope class. If triggered, it forces a large memory allocation / unguarded exception inside the TVM interpreter thread that is not caught locally, risking `OutOfMemoryError` propagation that can destabilize node processing of the transaction (potential node-wide DoS via memory pressure/GC storms, or an uncaught error depending on how the VM's outer dispatch layer in `Program.java`/interpreter handles it).

### Likelihood Explanation
The vulnerable branch is only taken while the `allowTvmSelfdestructRestriction` hard-fork flag is not yet active on a given chain (it's a `DynamicPropertiesStore`/`ProposalUtil`-controlled hard-fork switch). On networks (e.g. Mainnet) where this switch has already been activated by committee proposal, the safe `extractSigArray` path is always used and the bug is not reachable. I was **not able to verify from the indexed code** whether this flag is already permanently active/hardcoded on current Mainnet or still configurable/pending on some networks (e.g. private/test networks, or chains that haven't executed the corresponding TIP proposal) — this materially affects real-world exploitability and I could not fully confirm it before running out of iterations.

### Recommendation
- Always enforce the `MAX_SIZE` bound on the decoded array length *before* calling `extractBytesArray`, independent of `allowTvmSelfdestructRestriction`, e.g. read `words[words[3].intValueSafe() / WORD_SIZE].intValueSafe()` and reject if it exceeds `MAX_SIZE` unconditionally.
- Wrap the entire `ValidateMultiSign.execute()` body (not just the weight-calculation block) in a `try { ... } catch (Throwable t)` as is already done in `BatchValidateSign.execute()`, so any allocation/index error fails safely with `DATA_FALSE` instead of propagating.
- Add an explicit upper bound check inside `extractBytesArray`/`extractBytes32Array` themselves (defense in depth), rejecting `len` values above a small sane constant before allocating `new byte[len][]`.

### Proof of Concept
1. Ensure the target chain has not activated `allowTvmSelfdestructRestriction` (legacy path).
2. Deploy any contract, or use an existing one, that performs a low-level `call`/`staticcall` to the `validatemultisign` precompile address with calldata shaped as:
   - word0: target address
   - word1: permissionId
   - word2: hash
   - word3: offset to the bytes[] array
   - at that offset: a length word set to a very large value (e.g. `0x7FFFFFFF`)
3. Submit the `TriggerSmartContract` transaction. `extractBytesArray` executes `new byte[0x7FFFFFFF][]` before any `MAX_SIZE` check, throwing an unguarded `OutOfMemoryError`/exception inside `ValidateMultiSign.execute()`.

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

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1051-1080)
```java
    @Override
    public Pair<Boolean, byte[]> execute(byte[] rawData) {
      if (VMConfig.allowTvmOsaka()
          && !isValidAbiEncoding(rawData, ABI_HEADER_WORDS, ABI_ITEM_WORDS)) {
        return Pair.of(false, EMPTY_BYTE_ARRAY);
      }
      DataWord[] words = DataWord.parseArray(rawData);
      byte[] address = words[0].toTronAddress();
      int permissionId = words[1].intValueSafe();
      byte[] data = words[2].getData();

      byte[] combine = ByteUtil.merge(address, ByteArray.fromInt(permissionId), data);
      byte[] hash = Sha256Hash.hash(CommonParameter
          .getInstance().isECKeyCryptoEngine(), combine);

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

      AccountCapsule account = this.getDeposit().getAccount(address);
```
