### Title
ECDSA Signature Malleability in `ECRecover` Precompile Allows Non-Canonical (High-S) Signatures - (File: `actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java`)

### Summary
The `ECRecover` precompiled contract (TVM address `0x01`, invoked by the Solidity `ecrecover()` builtin) and the underlying `ECKey.ECDSASignature.validateComponents()` routine accept any `s` value in the full range `[1, N-1]` instead of enforcing the canonical low-`s` form (`s <= N/2`). This is the same bug class flagged in the external report: OpenZeppelin's pre-4.7.3 `ECDSA.recover()` was vulnerable because it accepted both the low-`s` and high-`s` (`N - s`) representations of a signature, which are two different byte-encodings that recover to the same signer/address for the same message.

### Finding Description
`PrecompiledContracts.ECRecover.execute()` parses `h, v, r, s` from the calldata and calls `SignUtils.fromComponents(...)` to build a `SignatureInterface`, then gates recovery only on `validateV(v)` and `signature.validateComponents()`: [1](#0-0) 

`validateComponents()` delegates to `ECKey.ECDSASignature.validateComponents(BigInteger r, BigInteger s, byte v)`, which only checks that `v` is 27/28 and that `r` and `s` are each within `[1, SECP256K1N - 1]` — it never rejects `s > HALF_CURVE_ORDER`: [2](#0-1) 

A canonicalization helper (`toCanonicalised()`) exists in the same class but is never invoked from the recovery/validation path: [3](#0-2) 

Because ECDSA signatures are symmetric under `(r, s) ↔ (r, N-s)` (with the `v`/recovery id flipped accordingly), for any valid signature `(r, s, v)` an attacker can compute a second, distinct byte-level signature `(r, N-s, v')` that recovers to the exact same address for the exact same message hash. Since `ECRecover.execute()` and `validateComponents()` accept both forms, any Solidity contract deployed on the TVM that uses `ecrecover()` (directly or via a library) for signature-based authorization, meta-transaction relay, permit-style approvals, order signing, or replay-protection keyed off the signature bytes/hash is exposed to malleability. Also note the manual `s`-length copy in the precompile: [4](#0-3) 

which further reflects the historically unhardened, "raw ECDSA.recover-style" input handling that OpenZeppelin's advisory GHSA-4h98-2769-gh6h specifically patched.

### Impact Explanation
Any deployed contract on java-tron that relies on `ecrecover()` for off-chain-signed authorization and uses the signature (or a hash containing the signature) as a uniqueness/replay-protection key is vulnerable to signature malleability: an unprivileged transaction broadcaster/contract caller can derive a second valid signature for an already-used message and resubmit it. Depending on the calling contract's logic this enables replay of one-time authorizations (e.g., double execution of a meta-tx, double-claiming a signed voucher, or bypassing a "signature already used" nonce check keyed by signature hash), i.e., unauthorized account operation / theft or double-spend of value gated by an ecrecover-based check. The precompile itself does not directly move funds, but it is the trust anchor most contract-level signature verification schemes on the TVM rely on, so the practical impact is inherited by whatever contract logic depends on it.

### Likelihood Explanation
Reachable by any contract deployer/caller with no special privileges: a smart contract simply needs to call `ecrecover()`, which routes to this same `ECRecover` precompile for every user of the chain. Exploitation requires only that some deployed contract's business logic treats signature malleability as security-relevant (e.g., replay protection tied to signature bytes/hash rather than to a nonce) — a common but not universal contract pattern. This is Medium likelihood: the vulnerability is trivially and cheaply reachable, but actual fund-impact depends on downstream contract design.

### Recommendation
Enforce canonical (low-`s`) signatures in `ECKey.ECDSASignature.validateComponents()` (and the SM2 equivalent) by rejecting `s > HALF_CURVE_ORDER`, mirroring OpenZeppelin's fix in `ECDSA.tryRecover`. Apply the same check inside `PrecompiledContracts.ECRecover.execute()` and `recoverAddrBySign()` before performing recovery, so that only the canonical low-`s` encoding of a signature is ever accepted by the TVM `ecrecover` precompile.

### Proof of Concept
1. Generate a valid ECDSA signature `(r, s, v)` over a message hash `h` with a test key.
2. Compute the malleable counterpart `s' = SECP256K1N - s` and the flipped recovery id `v' = v XOR 1` (27↔28).
3. Call the `ECRecover` precompile (or a Solidity contract calling `ecrecover(h, v, r, s)` and separately `ecrecover(h, v', r, s')`) — both calls, evaluated via `PrecompiledContracts.ECRecover.execute()` at [5](#0-4) , return the identical recovered address, confirming two distinct signature byte-strings are both accepted as valid for the same signer/message — the definition of signature malleability.

### Citations

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L598-630)
```java
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

**File:** crypto/src/main/java/org/tron/common/crypto/ECKey.java (L923-946)
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


    public boolean validateComponents() {
      return validateComponents(r, s, v);
    }
```

**File:** crypto/src/main/java/org/tron/common/crypto/ECKey.java (L948-963)
```java
    public ECDSASignature toCanonicalised() {
      if (s.compareTo(HALF_CURVE_ORDER) > 0) {
        // The order of the curve is the number of valid points that
        // exist on that curve. If S is in the upper
        // half of the number of valid points, then bring it back to
        // the lower half. Otherwise, imagine that
        //    N = 10
        //    s = 8, so (-8 % 10 == 2) thus both (r, 8) and (r, 2)
        // are valid solutions.
        //    10 - 8 == 2, giving us always the latter solution,
        // which is canonical.
        return new ECDSASignature(r, CURVE.getN().subtract(s));
      } else {
        return this;
      }
    }
```
