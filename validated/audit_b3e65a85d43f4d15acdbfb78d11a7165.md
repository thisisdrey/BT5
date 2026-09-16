Found a strong analog: `PrecompiledContracts.ECRecover.execute` performs unguarded, offset-based `System.arraycopy` reads on attacker-controlled `data` without validating the total input length before the copies — the same "fixed-offset field read from an under-sized buffer" bug class as FreeRDP's `license_read_new_or_upgrade_license_packet`.

### Title
Out-of-bounds read in TVM `ECRecover` precompile due to unchecked fixed-offset `arraycopy` reads - (File: `actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java`)

### Summary
`PrecompiledContracts.ECRecover.execute(byte[] data)` copies four 32-byte fields (`h`, `v`, `r`, `s`) out of the untrusted `data` buffer at fixed offsets 0/32/64/96, mirroring the CVE-2020-11099 pattern where a fixed-format packet's sub-fields are read from an attacker buffer without first checking that the buffer is long enough to contain them.

### Finding Description
`execute` does: [1](#0-0) 
```
System.arraycopy(data, 0, h, 0, 32);
System.arraycopy(data, 32, v, 0, 32);
System.arraycopy(data, 64, r, 0, 32);
int sLength = data.length < 128 ? data.length - 96 : 32;
System.arraycopy(data, 96, s, 0, sLength);
```
There is no upfront check that `data.length >= 32` before the first `arraycopy(data,0,h,0,32)`, and no check that `data.length >= 64`/`96` before the second/third copies. If `data` is shorter than 32 bytes (e.g., empty or a few bytes), the very first `arraycopy` throws `ArrayIndexOutOfBoundsException`. Also, if `96 <= data.length < 96` is not possible, but for `data.length` between 96 and 127 the computed `sLength = data.length - 96` can be 0 or a small positive number — while for `data.length < 96` (but the code already unconditionally executed `arraycopy(data, 64, r, 0, 32)` beforehand, which itself throws when `data.length < 96`). This is functionally identical to the FreeRDP flaw: a fixed multi-field struct is parsed from a buffer whose length was never validated against the total struct size before individual field reads begin.

Reachability: `data` is the calldata passed to the `ecrecover` precompile at address `0x01`, fully controlled by any contract caller. Any user can invoke a smart contract that calls `ecrecover(bytes)` (or directly craft a `TriggerSmartContract` call to the precompile address) with `data.length < 96`, triggering the uncaught-until-caller `ArrayIndexOutOfBoundsException`.

### Impact Explanation
The exception is caught by a broad `catch (Throwable any) {}` inside `execute`, so the immediate effect is precompile execution failing silently and `out` remaining `null`, returning `Pair.of(true, EMPTY_BYTE_ARRAY)` — i.e., the call succeeds but returns empty output instead of reverting, which can silently corrupt the semantics of contracts relying on `ecrecover`'s return convention (32-byte zero-padded address expected, but empty bytes returned). This does not crash the node (the `Throwable` catch absorbs it) but produces incorrect execution results across all nodes deterministically (since TVM execution is consensus-critical), which does not itself cause a consensus split since all nodes compute the same wrong result. The primary confirmed impact is API/precompile misbehavior (returning wrong result silently) rather than node crash, given the catch-all.

### Likelihood Explanation
Trivial to trigger — any account can deploy or call a contract that invokes `ecrecover` with truncated data, or call the precompile address directly via `TriggerSmartContract`. No privilege required.

### Recommendation
Add an explicit length check at the start of `ECRecover.execute` (e.g., `if (data == null || data.length < 128) { pad/truncate safely }`) before performing any `arraycopy`, following the same right-padding approach used elsewhere in the codebase (see `ByteUtil.parseBytes`/`parseWord`) [2](#0-1) , so truncated/short calldata is handled deterministically without relying on an exception being thrown and swallowed.

### Proof of Concept
Deploy a contract that calls the `ecrecover` precompile (address `0x01`) with calldata shorter than 32 bytes, e.g. via low-level `call(gas, 0x01, 0, data, 10, output, 32)` where `data.length == 10`. `PrecompiledContracts.ECRecover.execute` will attempt `System.arraycopy(data, 0, h, 0, 32)` on a 10-byte array, throwing `ArrayIndexOutOfBoundsException`, caught by the blanket `catch (Throwable any) {}`, and the call silently returns empty output instead of a defined revert/zero-address result — demonstrating the unchecked fixed-offset read analogous to the FreeRDP license-packet OOB read. [3](#0-2)

### Citations

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L582-630)
```java
  public static class ECRecover extends PrecompiledContract {

    private static boolean validateV(byte[] v) {
      for (int i = 0; i < v.length - 1; i++) {
        if (v[i] != 0) {
          return false;
        }
      }
      return true;
    }

    @Override
    public long getEnergyForData(byte[] data) {
      return 3000;
    }

    @Override
    public Pair<Boolean, byte[]> execute(byte[] data) {

      byte[] h = new byte[32];
      byte[] v = new byte[32];
      byte[] r = new byte[32];
      byte[] s = new byte[32];

      DataWord out = null;

      try {
        System.arraycopy(data, 0, h, 0, 32);
        System.arraycopy(data, 32, v, 0, 32);
        System.arraycopy(data, 64, r, 0, 32);

        int sLength = data.length < 128 ? data.length - 96 : 32;
        System.arraycopy(data, 96, s, 0, sLength);

        SignatureInterface signature = SignUtils.fromComponents(r, s, v[31]
            , CommonParameter.getInstance().isECKeyCryptoEngine());
        if (validateV(v) && signature.validateComponents()) {
          out = new DataWord(SignUtils.signatureToAddress(h, signature
              , CommonParameter.getInstance().isECKeyCryptoEngine()));
        }
      } catch (Throwable any) {
      }

      if (out == null) {
        return Pair.of(true, EMPTY_BYTE_ARRAY);
      } else {
        return Pair.of(true, out.getData());
      }
    }
```

**File:** common/src/main/java/org/tron/common/utils/ByteUtil.java (L344-353)
```java
  public static byte[] parseBytes(byte[] input, int offset, int len) {

    if (offset >= input.length || len == 0) {
      return EMPTY_BYTE_ARRAY;
    }

    byte[] bytes = new byte[len];
    System.arraycopy(input, offset, bytes, 0, min(input.length - offset, len, true));
    return bytes;
  }
```
