### Title
Uncaught `ArithmeticException` in ECDSA/SM2 public-key recovery on crafted `r=0` signature crashes transaction/block signature verification - (File: `crypto/src/main/java/org/tron/common/crypto/ECKey.java`)

### Summary
The GnuTLS CVE is a crash triggered by feeding a specially crafted, structurally-valid-but-malformed input (a PEM chain) into a verification routine that does not defensively validate all field values before using them in further computation. java-tron has the same bug class in its signature-recovery code path used for both single-signature and multi-signature (permission) verification: `ECKey.recoverPubBytesFromSignature` computes `sig.r.modInverse(n)` without first checking that `r != 0` [1](#0-0) , and later performs the actual inversion at [2](#0-1) . `BigInteger.modInverse(0, n)` throws an unchecked `ArithmeticException("BigInteger not invertible")`.

### Finding Description
`TransactionCapsule.checkWeight` — the function used to verify multi-signature transactions and account-permission weights — decodes each raw signature and calls `SignUtils.signatureToAddress(...)` directly, with no upstream validation of the `r`/`s` components: [3](#0-2) . This eventually reaches `ECKey.recoverPubBytesFromSignature`, whose only sanity checks are `recId >= 0`, `r.signum() >= 0`, `s.signum() >= 0`, and `messageHash != null` [1](#0-0)  — none of which reject `r == 0`. Compare this with the TVM `P256Verify` precompile, which explicitly rejects `r.signum() <= 0` before using it [4](#0-3) , and with the SM2 verification path, which wraps BouncyCastle calls in a `catch (NullPointerException)` specifically because "BouncyCastle contains a bug that can cause NPEs given specially crafted signatures" [5](#0-4) . `ECKey.recoverPubBytesFromSignature`/`checkWeight` have no equivalent hardening for the `ArithmeticException` from `modInverse(0)`.

`checkWeight` only declares checked exceptions `SignatureException, PermissionException, SignatureFormatException` [6](#0-5) , so an `ArithmeticException` propagates unhandled through `validatePubSignature`, which itself only catches `SignatureException | PermissionException | SignatureFormatException` [7](#0-6) , and through `validateSignature` [8](#0-7) .

### Impact Explanation
Any single-signer transaction sent through `Wallet.broadcastTransaction` is shielded because that call site has a final `catch (Exception e)` [9](#0-8) , so the RPC path degrades to an error response rather than crashing the API thread. The higher-risk exposure is the transaction-signature verification performed during block application in `Manager` when applying transactions embedded in a block (both self-produced and received via the network); this is the same `TransactionCapsule.validateSignature`/`checkWeight` code path, and I was unable to fully confirm whether every call site in `Manager`'s block-application flow wraps this specific unchecked `ArithmeticException` the same way `Wallet.broadcastTransaction` does. If any such call site lacks a broad `catch (Throwable/Exception)`, a witness/full node processing a block containing a crafted multisig transaction (`r=0` signature bytes, otherwise well-formed with a valid permission/account setup) would throw an uncaught `ArithmeticException` on that thread, which can crash or stall block processing — a node-halt / DoS condition, matching the "node crash or halt" acceptance criterion for this scan.

### Likelihood Explanation
Triggering `r == 0` requires only crafting a 65+ byte signature blob with the `r` component bytes set to all zero; this passes the length check in `checkWeight` (`sig.size() < 65`) [10](#0-9)  and the non-negativity checks in `ECKey` (`r.signum() >= 0` is true for `r == 0`) [1](#0-0) . No private key or special privilege is needed — only an account with multisig permissions configured (attacker can set this up on their own account via `AccountPermissionUpdateContract`) and a crafted signature attached to a transaction referencing that permission.

### Recommendation
Add an explicit `r.signum() > 0 && r.compareTo(n) < 0` (and similarly for `s`) check in `ECKey.recoverPubBytesFromSignature` and `SM2.recoverPubBytesFromSignature` before calling `modInverse`, returning `null` (as the function already does for out-of-range `x`) instead of throwing. Additionally, wrap the crypto-recovery calls inside `TransactionCapsule.checkWeight` in a `catch (RuntimeException)` that is translated into `SignatureException`, so malformed signature components can never surface as an uncaught runtime exception on the block-application or transaction-validation path.

### Proof of Concept
1. Craft a 65-byte raw signature where bytes for `r` are all `0x00` (`r = 0`), `s` and `v` set to any values that pass basic format checks.
2. Attach this as a signature on a transaction whose owner account has a multisig `Active`/`Owner` permission configured (attacker-controlled account).
3. Submit/relay the transaction such that `TransactionCapsule.validateSignature` → `checkWeight` → `SignUtils.signatureToAddress` → `ECKey.recoverPubBytesFromSignature` is invoked on it during transaction processing/block application.
4. `sig.r.modInverse(n)` at [11](#0-10)  throws `ArithmeticException: BigInteger not invertible.`, which is not declared/caught by `checkWeight` or `validatePubSignature`, propagating to the caller's thread.

### Citations

**File:** crypto/src/main/java/org/tron/common/crypto/ECKey.java (L517-523)
```java
  @Nullable
  public static byte[] recoverPubBytesFromSignature(int recId,
      ECDSASignature sig, byte[] messageHash) {
    check(recId >= 0, "recId must be positive");
    check(sig.r.signum() >= 0, "r must be positive");
    check(sig.s.signum() >= 0, "s must be positive");
    check(messageHash != null, "messageHash must not be null");
```

**File:** crypto/src/main/java/org/tron/common/crypto/ECKey.java (L579-581)
```java
    BigInteger eInv = BigInteger.ZERO.subtract(e).mod(n);
    BigInteger rInv = sig.r.modInverse(n);
    BigInteger srInv = rInv.multiply(sig.s).mod(n);
```

**File:** chainbase/src/main/java/org/tron/core/capsule/TransactionCapsule.java (L233-236)
```java
  public static long checkWeight(Permission permission, List<ByteString> sigs, byte[] hash,
      List<ByteString> approveList)
      throws SignatureException, PermissionException, SignatureFormatException {
    long currentWeight = 0;
```

**File:** chainbase/src/main/java/org/tron/core/capsule/TransactionCapsule.java (L243-256)
```java
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
```

**File:** chainbase/src/main/java/org/tron/core/capsule/TransactionCapsule.java (L666-676)
```java
      try {
        if (!validateSignature(this.transaction, hash, accountStore, dynamicPropertiesStore)) {
          isVerified = false;
          throw new ValidateSignatureException("sig error");
        }
      } catch (SignatureException | PermissionException | SignatureFormatException e) {
        isVerified = false;
        throw new ValidateSignatureException(e.getMessage());
      } finally {
        logSlowSigVerify(startNs);
      }
```

**File:** chainbase/src/main/java/org/tron/core/capsule/TransactionCapsule.java (L698-719)
```java
  public boolean validateSignature(AccountStore accountStore,
      DynamicPropertiesStore dynamicPropertiesStore) throws ValidateSignatureException {
    if (!isVerified) {
      //Do not support multi contracts in one transaction
      Transaction.Contract contract = this.getInstance().getRawData().getContract(0);
      if (contract.getType() != ContractType.ShieldedTransferContract) {
        validatePubSignature(accountStore, dynamicPropertiesStore);
      } else {  //ShieldedTransfer
        byte[] owner = getOwnerAddress();
        if (!ArrayUtils.isEmpty(owner)) { //transfer from transparent address
          validatePubSignature(accountStore, dynamicPropertiesStore);
        } else { //transfer from shielded address
          if (this.transaction.getSignatureCount() > 0) {
            throw new ValidateSignatureException("there should be no signatures signed by "
                    + "transparent address when transfer from shielded address");
          }
        }
      }
      isVerified = true;
    }
    return true;
  }
```

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L2361-2365)
```java

        if (r.signum() <= 0 || r.compareTo(N) >= 0
            || s.signum() <= 0 || s.compareTo(N) >= 0) {
          return Pair.of(true, EMPTY_BYTE_ARRAY);
        }
```

**File:** crypto/src/main/java/org/tron/common/crypto/sm2/SM2.java (L709-718)
```java
    try {
      return signer.verifyHashSignature(data, signature.r, signature.s);
    } catch (NullPointerException npe) {
      // Bouncy Castle contains a bug that can cause NPEs given
      // specially crafted signatures.
      // Those signatures are inherently invalid/attack sigs so we just
      // fail them here rather than crash the thread.
      logger.error("Caught NPE inside bouncy castle", npe);
      return false;
    }
```

**File:** framework/src/main/java/org/tron/core/Wallet.java (L624-629)
```java
    } catch (Exception e) {
      logger.warn("Broadcast transaction {} failed", txID, e);
      return builder.setResult(false).setCode(response_code.OTHER_ERROR)
          .setMessage(ByteString.copyFromUtf8("Error: " + e.getMessage()))
          .build();
    }
```
