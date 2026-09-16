## Title
Missing canonical range validation (`r`/`s` < curve order) in the core transaction signature-recovery path `TransactionCapsule.checkWeight` — (`File: chainbase/src/main/java/org/tron/core/capsule/TransactionCapsule.java`)

### Summary
The CVE-2021-38195 bug class is "signature verification accepts R or S larger than the curve order." java-tron has an internal helper, `ECDSASignature.validateComponents()`, that performs exactly this bounds check, and it is *correctly* invoked by the TVM `ECRecover` precompile and the `recoverAddrBySign` helper. However, the consensus-critical multi-signature verification path `TransactionCapsule.checkWeight()` — which authorizes every broadcast transaction against an account's `Permission` — recovers the signer address via `SignUtils.signatureToAddress()` → `ECKey.signatureToAddress()` → `recoverPubBytesFromSignature()` **without ever calling `validateComponents()`**, so `r`/`s` are never checked against the curve order `N` before being used in signature recovery.

### Finding Description
`ECKey.ECDSASignature.validateComponents()` enforces `1 <= r < N` and `1 <= s < N` (and `v ∈ {27,28}`): [1](#0-0) 

This check is applied in the TVM `ECRecover` precompile: [2](#0-1) 

and in the `recoverAddrBySign` helper used elsewhere in `PrecompiledContracts`: [3](#0-2) 

But the primary consensus signature-verification routine, `TransactionCapsule.checkWeight()` — invoked to authorize every signed transaction against multi-sig `Permission` weights — calls `SignUtils.signatureToAddress(hash, base64, ...)` directly, with no call to `validateComponents()` anywhere in the loop: [4](#0-3) 

That call chain goes through `SignUtils.signatureToAddress` → `ECKey.signatureToAddress` → `signatureToKeyBytes` → `recoverPubBytesFromSignature`: [5](#0-4) [6](#0-5) 

`recoverPubBytesFromSignature` only asserts `r`/`s` are non-negative — it never checks the upper bound against the curve order `N` (or the field prime `P`) before doing modular arithmetic (`sig.r.modInverse(n)`, `x = sig.r.add(i.multiply(n))`): [7](#0-6) 

Because `x` and `rInv`/`srInv` are computed with raw (unreduced) `r`/`s` values, a caller can supply `r` or `s` in the range `[N, P)` — a value that is not a canonical ECDSA signature component but is still small enough to be a valid curve x-coordinate/mod-n operand — and the recovery succeeds, silently doing arithmetic modulo `N` on out-of-range inputs. This is precisely the "R or S larger than curve order" overflow condition described in the advisory.

### Impact Explanation
`checkWeight` is the authorization gate for every multi-signature-permissioned transaction (transfers, TRC-10/TRC-20 operations, witness/committee proposals, exchange orders, etc. — any actuator invoked through a signed `Transaction`). Because this path skips `validateComponents()`, malformed/non-canonical signature encodings (out-of-range `r`/`s`) are accepted into consensus-critical signature recovery instead of being rejected outright, unlike the TVM precompile path which explicitly guards against this. This inconsistency:
- Allows non-canonical/malleable signature byte-strings to be treated as valid, undermining any invariant that assumes signature-byte uniqueness per signer (e.g., replay/dedup logic keyed on raw signature bytes elsewhere in the system).
- Breaks defense-in-depth: the two call paths for the "same" ECDSA verification logic (precompile vs. core transaction authorization) diverge in strictness, meaning a bug class already known to be dangerous (CVE-2021-38195) and already mitigated in one place in this codebase is left unmitigated in the actual chain-authorization path.

### Likelihood Explanation
Any unprivileged transaction broadcaster can submit a `Transaction` with a manipulated raw signature buffer; `checkWeight` is reached for any account with `AllowMultiSign` permissions, which is a normal, attacker-reachable feature, not privileged.

### Recommendation
Call `SignatureInterface.validateComponents()` (or an equivalent `r < N`, `s < N`, `r,s >= 1` check) before/after signature recovery inside `TransactionCapsule.checkWeight()`, mirroring the check already performed in `PrecompiledContracts.ECRecover` and `recoverAddrBySign`, so out-of-range `r`/`s` values are rejected with a `SignatureFormatException`/`PermissionException` rather than silently processed by `recoverPubBytesFromSignature`.

### Proof of Concept
1. Construct a transaction `raw` and hash it (`hash`).
2. Craft a 65-byte signature blob `[v][r][s]` where `r` (or `s`) is set to a value in `[N, P)` (i.e., `>= FFFFFFFF...FEBAAEDCE6AF48A03BBFD25E8CD0364141` but `< FFFFFFFF...FFFFFFFEFFFFFC2F`), leaving `v` valid (27/28).
3. Call `TransactionCapsule.checkWeight(permission, sigs, hash, null)` with this signature — observe that no `validateComponents()`-style rejection occurs before `recoverPubBytesFromSignature` performs modular arithmetic on the out-of-range component, unlike calling `PrecompiledContracts.ECRecover.execute()` with the same malformed `r`/`s`, which correctly returns empty output due to its explicit `signature.validateComponents()` guard.

### Citations

**File:** crypto/src/main/java/org/tron/common/crypto/ECKey.java (L411-434)
```java
  public static byte[] signatureToKeyBytes(byte[] messageHash,
      ECDSASignature sig) throws SignatureException {
    check(messageHash.length == 32, "messageHash argument has length " +
        messageHash.length);
    int header = sig.v;
    // The header byte: 0x1B = first key with even y, 0x1C = first key
    // with odd y,
    //                  0x1D = second key with even y, 0x1E = second key
    // with odd y
    if (header < 27 || header > 34) {
      throw new SignatureException("Header byte out of range: " + header);
    }
    if (header >= 31) {
      header -= 4;
    }
    int recId = header - 27;
    byte[] key = ECKey.recoverPubBytesFromSignature(recId, sig,
        messageHash);
    if (key == null) {
      throw new SignatureException("Could not recover public key from " +
          "signature");
    }
    return key;
  }
```

**File:** crypto/src/main/java/org/tron/common/crypto/ECKey.java (L517-586)
```java
  @Nullable
  public static byte[] recoverPubBytesFromSignature(int recId,
      ECDSASignature sig, byte[] messageHash) {
    check(recId >= 0, "recId must be positive");
    check(sig.r.signum() >= 0, "r must be positive");
    check(sig.s.signum() >= 0, "s must be positive");
    check(messageHash != null, "messageHash must not be null");
    // 1.0 For j from 0 to h   (h == recId here and the loop is outside
    // this function)
    //   1.1 Let x = r + jn
    BigInteger n = CURVE.getN();  // Curve order.
    BigInteger i = BigInteger.valueOf((long) recId / 2);
    BigInteger x = sig.r.add(i.multiply(n));
    //   1.2. Convert the integer x to an octet string X of length mlen
    // using the conversion routine
    //        specified in Section 2.3.7, where mlen = ⌈(log2 p)/8⌉ or
    // mlen = ⌈m/8⌉.
    //   1.3. Convert the octet string (16 set binary digits)||X to an
    // elliptic curve point R using the
    //        conversion routine specified in Section 2.3.4. If this
    // conversion routine outputs “invalid”, then
    //        do another iteration of Step 1.
    //
    // More concisely, what these points mean is to use X as a compressed
    // public key.
    ECCurve.Fp curve = (ECCurve.Fp) CURVE.getCurve();
    BigInteger prime = curve.getQ();  // Bouncy Castle is not consistent
    // about the letter it uses for the prime.
    if (x.compareTo(prime) >= 0) {
      // Cannot have point co-ordinates larger than this as everything
      // takes place modulo Q.
      return null;
    }
    // Compressed allKeys require you to know an extra bit of data about the
    // y-coord as there are two possibilities.
    // So it's encoded in the recId.
    ECPoint R = decompressKey(x, (recId & 1) == 1);
    //   1.4. If nR != point at infinity, then do another iteration of
    // Step 1 (callers responsibility).
    if (!R.multiply(n).isInfinity()) {
      return null;
    }
    //   1.5. Compute e from M using Steps 2 and 3 of ECDSA signature
    // verification.
    BigInteger e = new BigInteger(1, messageHash);
    //   1.6. For k from 1 to 2 do the following.   (loop is outside this
    // function via iterating recId)
    //   1.6.1. Compute a candidate public key as:
    //               Q = mi(r) * (sR - eG)
    //
    // Where mi(x) is the modular multiplicative inverse. We transform
    // this into the following:
    //               Q = (mi(r) * s ** R) + (mi(r) * -e ** G)
    // Where -e is the modular additive inverse of e, that is z such that
    // z + e = 0 (mod n). In the above equation
    // ** is point multiplication and + is point addition (the EC group
    // operator).
    //
    // We can find the additive inverse by subtracting e from zero then
    // taking the mod. For example the additive
    // inverse of 3 modulo 11 is 8 because 3 + 8 mod 11 = 0, and -3 mod
    // 11 = 8.
    BigInteger eInv = BigInteger.ZERO.subtract(e).mod(n);
    BigInteger rInv = sig.r.modInverse(n);
    BigInteger srInv = rInv.multiply(sig.s).mod(n);
    BigInteger eInvrInv = rInv.multiply(eInv).mod(n);
    ECPoint.Fp q = (ECPoint.Fp) ECAlgorithms.sumOfTwoMultiplies(CURVE
        .getG(), eInvrInv, R, srInv);
    return q.getEncoded(/* compressed */ false);
  }
```

**File:** crypto/src/main/java/org/tron/common/crypto/ECKey.java (L923-941)
```java
    public static boolean validateComponents(BigInteger r, BigInteger s,
        byte v) {

      if (v != 27 && v != 28) {
        return false;
      }

      if (BIUtil.isLessThan(r, BigInteger.ONE)) {
        return false;
      }
      if (BIUtil.isLessThan(s, BigInteger.ONE)) {
        return false;
      }

      if (!BIUtil.isLessThan(r, SECP256K1N)) {
        return false;
      }
      return BIUtil.isLessThan(s, SECP256K1N);
    }
```

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L376-383)
```java
    try {
      Rsv rsv = Rsv.fromSignature(sign);
      SignatureInterface signature = SignUtils.fromComponents(rsv.getR(), rsv.getS(), rsv.getV(),
          CommonParameter.getInstance().isECKeyCryptoEngine());
      if (signature.validateComponents()) {
        out = SignUtils.signatureToAddress(hash, signature,
            CommonParameter.getInstance().isECKeyCryptoEngine());
      }
```

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L616-621)
```java
        SignatureInterface signature = SignUtils.fromComponents(r, s, v[31]
            , CommonParameter.getInstance().isECKeyCryptoEngine());
        if (validateV(v) && signature.validateComponents()) {
          out = new DataWord(SignUtils.signatureToAddress(h, signature
              , CommonParameter.getInstance().isECKeyCryptoEngine()));
        }
```

**File:** chainbase/src/main/java/org/tron/core/capsule/TransactionCapsule.java (L233-270)
```java
  public static long checkWeight(Permission permission, List<ByteString> sigs, byte[] hash,
      List<ByteString> approveList)
      throws SignatureException, PermissionException, SignatureFormatException {
    long currentWeight = 0;
    if (sigs.size() > permission.getKeysCount()) {
      throw new PermissionException(
          "Signature count is " + (sigs.size()) + " more than key counts of permission : "
              + permission.getKeysCount());
    }
    HashMap addMap = new HashMap();
    for (ByteString sig : sigs) {
      if (sig.size() < 65) {
        throw new SignatureFormatException(
            "Signature size is " + sig.size());
      }
      String base64 = TransactionCapsule.getBase64FromByteString(sig);
      byte[] address = SignUtils
          .signatureToAddress(hash, base64, CommonParameter.getInstance().isECKeyCryptoEngine());
      long weight = getWeight(permission, address);
      if (weight == 0) {
        throw new PermissionException(
            ByteArray.toHexString(hash) + " is signed by " + encode58Check(address)
                + " but it is not contained of permission.");
      }
      if (ForkController.instance().pass(Parameter.ForkBlockVersionEnum.VERSION_4_7_1)) {
        base64 = encode58Check(address);
      }
      if (addMap.containsKey(base64)) {
        throw new PermissionException(encode58Check(address) + " has signed twice!");
      }
      addMap.put(base64, weight);
      if (approveList != null) {
        approveList.add(ByteString.copyFrom(address)); //out put approve list.
      }
      currentWeight += weight;
    }
    return currentWeight;
  }
```

**File:** crypto/src/main/java/org/tron/common/crypto/SignUtils.java (L44-55)
```java
  public static byte[] signatureToAddress(
      byte[] messageHash, String signatureBase64, boolean isECKeyCryptoEngine)
      throws SignatureException {
    try {
      if (isECKeyCryptoEngine) {
        return ECKey.signatureToAddress(messageHash, signatureBase64);
      }
      return SM2.signatureToAddress(messageHash, signatureBase64);
    } catch (Exception e) {
      throw new SignatureException(e);
    }
  }
```
