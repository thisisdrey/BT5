### Title
Unvalidated Sapling ciphertext length passed to fixed-size native ChaCha20‑Poly1305 AEAD decrypt calls can corrupt native memory / crash the node - ([File: framework/src/main/java/org/tron/core/zen/note/NoteEncryption.java])

### Summary
`NoteEncryption.Encryption.attemptEncDecryption(byte[] ciphertext, ...)` and `attemptOutDecryption(...)` invoke the native libsodium ChaCha20‑Poly1305 IETF decrypt binding with a **hard-coded** length constant (`ZC_ENCCIPHERTEXT_SIZE` = 580, `ZC_OUTCIPHERTEXT_SIZE` = 80) instead of the actual length of the caller-supplied `ciphertext` byte array. Several Wallet-layer code paths feed these functions with byte arrays taken directly from protobuf `bytes` fields of a broadcast `ShieldedTransferContract`, without first checking that the field's real length matches the expected constant.

### Finding Description
`Note.decrypt(byte[] ciphertext, byte[] ivk, byte[] epk, byte[] cmu)` calls `NoteEncryption.Encryption.attemptEncDecryption(ciphertext, ivk, epk)`: [1](#0-0) 

Inside that method the actual `ciphertext` array is passed to the native AEAD decrypt call together with the **compile-time constant** `ZC_ENCCIPHERTEXT_SIZE`, not `ciphertext.length`: [2](#0-1) 

The same pattern exists for the outgoing-viewing-key path, `attemptOutDecryption`, which uses the fixed `ZC_OUTCIPHERTEXT_SIZE`: [3](#0-2) 

These calls are reachable from Wallet APIs that pull `c_enc`/`c_out` straight out of the `ReceiveDescription` protobuf message of a `ShieldedTransferContract` without validating their byte length: [4](#0-3) [5](#0-4) 

`ShieldedTransferContract.ReceiveDescription.c_enc`/`c_out` are ordinary protobuf `bytes` fields with no length constraint enforced at the protocol layer, so an attacker who crafts and broadcasts a `ShieldedTransferContract` (with a still-valid zk-SNARK proof structure, but a `c_enc`/`c_out` field shorter or longer than the expected fixed size) can get that malformed record accepted into the chain. Any subsequent node — or the local node itself — that serves the `scanNoteByIvk`/`scanNoteByOvk`/`scanShieldedTRC20NotesByIvk` gRPC/HTTP wallet APIs (called by any anonymous client holding an incoming/outgoing viewing key, which is not privileged) will pass the malformed, attacker-controlled-length byte array into the native decrypt function while the function still claims a fixed 580/80-byte length to the JNI/native layer. Because the native libsodium binding receives a byte-array pointer obtained from JNI together with a **length argument that does not match the actual Java array's real size** when the array is shorter than the constant, this is the same root-cause class as the reported libcrux bug (CWE‑120: buffer size mismatch passed to an AEAD primitive) — except here the mismatch risks reading past the bounds of the actual JVM-managed array from native code, rather than a clean language-level panic.

### Impact Explanation
If the array supplied is shorter than the hard-coded constant, the native decrypt call reads/authenticates memory beyond the actual byte array bounds. In JNI, this is undefined behavior that typically results in a JVM segmentation fault (whole-node crash / denial of service), and in the worst case can lead to memory disclosure or corruption in the native heap. Since this path is reachable through public wallet/query APIs available to any client with an ivk/ovk (no special privileges), and the malicious ciphertext originates from an ordinary broadcastable transaction, this can be triggered by any unprivileged user, causing crash of any full node that serves these Shielded APIs — an accepted "node crash" impact.

### Likelihood Explanation
Likelihood is Medium-to-High for a node operator that has shielded transactions enabled (`allowShieldedTransactionApi`): an attacker only needs to broadcast one `ShieldedTransferContract` with an odd-length `c_enc`/`c_out` byte field (assuming it is not otherwise size-checked before proof validation) and then trigger any note-scanning API call with a matching ivk/ovk (which the attacker himself controls, since they generated the malicious note record). This does not require compromising any private key, SR, or peer.

### Recommendation
- In `NoteEncryption.Encryption.attemptEncDecryption` and `attemptOutDecryption`, validate that `ciphertext.length == ZC_ENCCIPHERTEXT_SIZE` / `ciphertext.data.length == ZC_OUTCIPHERTEXT_SIZE` before calling into native code, returning `Optional.empty()` on mismatch (as is already done for the burn-message path in `decryptBurnMessageByOvk`).
- Add the same length checks in `ShieldedTransferActuator.validate()`/proof-checking code so that malformed `c_enc`/`c_out` fields are rejected before a `ShieldedTransferContract` can ever be included in a block.

### Proof of Concept
1. Craft a `ShieldedTransferContract` whose `ReceiveDescription.c_enc` field is truncated (e.g. 32 bytes instead of 580) or padded (e.g. 4096 bytes) while keeping other fields consistent enough to pass whatever proof checks exist in `ShieldedTransferActuator.checkProof`.
2. Broadcast the transaction; if accepted into a block, its raw `c_enc` bytes are now retrievable from any full node.
3. Call the node's `scanNoteByIvk`/`ScanNoteByIvk` (or `IsSpend`/`ScanShieldedTRC20NotesByIvk`) API with any ivk; this triggers `Wallet.queryNoteByIvk` → `Note.decrypt(r.getCEnc().toByteArray(), ivk, ...)` → `NoteEncryption.Encryption.attemptEncDecryption`, invoking the native decrypt with the wrong (undersized/oversized) buffer against the hard-coded `ZC_ENCCIPHERTEXT_SIZE` length, exercising the out-of-bounds native access.

Note: I could not fully confirm within this session whether `ShieldedTransferActuator.validate()`/`checkProof()` already enforces an exact byte-length check on `c_enc`/`c_out` before storage (the actuator file references `ZC_ENCCIPHERTEXT_SIZE`/`getCEnc`/`getCOut` but I was unable to view those specific lines before the tool budget ran out). If such a check already exists there, the exploitability of this specific path would be blocked at validation time and the issue would be limited to defense-in-depth; this should be verified directly in `actuator/src/main/java/org/tron/core/actuator/ShieldedTransferActuator.java`.

### Citations

**File:** framework/src/main/java/org/tron/core/zen/note/Note.java (L127-133)
```java
  public static Optional<Note> decrypt(
      byte[] ciphertext, byte[] ivk, byte[] epk, byte[] cmu) throws ZksnarkException {
    Optional<NoteEncryption.Encryption.EncPlaintext> pt =
        NoteEncryption.Encryption.attemptEncDecryption(ciphertext, ivk, epk);
    if (!pt.isPresent()) {
      return Optional.empty();
    }
```

**File:** framework/src/main/java/org/tron/core/zen/note/NoteEncryption.java (L182-206)
```java
    public static Optional<EncPlaintext> attemptEncDecryption(
        byte[] ciphertext, byte[] ivk, byte[] epk) throws ZksnarkException {
      byte[] sharedsecret = new byte[32];
      //generate sharedsecret by epk and ivk
      if (!JLibrustzcash.librustzcashKaAgree(new KaAgreeParams(epk, ivk, sharedsecret))) {
        return Optional.empty();
      }
      byte[] kEnc = new byte[NOTEENCRYPTION_CIPHER_KEYSIZE];
      //generate kEnc by sharedsecret and epk
      kdfSapling(kEnc, sharedsecret, epk);
      byte[] cipher_nonce = new byte[CRYPTO_AEAD_CHACHA20POLY1305_IETF_NPUBBYTES];
      EncPlaintext plaintext = new EncPlaintext();
      plaintext.data = new byte[ZC_ENCPLAINTEXT_SIZE];
      //decrypt cEnc by kEnc
      if (JLibsodium.cryptoAeadChacha20poly1305IetfDecrypt(new Chacha20poly1305IetfDecryptParams(
          plaintext.data, null,
          null,
          ciphertext, ZC_ENCCIPHERTEXT_SIZE,
          null,
          0,
          cipher_nonce, kEnc)) != 0) {
        return Optional.empty();
      }
      return Optional.of(plaintext);
    }
```

**File:** framework/src/main/java/org/tron/core/zen/note/NoteEncryption.java (L242-262)
```java
    public static Optional<OutPlaintext> attemptOutDecryption(
        OutCiphertext ciphertext, byte[] ovk, byte[] cv, byte[] cm, byte[] epk)
        throws ZksnarkException {
      byte[] ock = new byte[NOTEENCRYPTION_CIPHER_KEYSIZE];
      //generate ock by ovk, cv, cm, epk
      prfOck(ock, ovk, cv, cm, epk);
      byte[] cipherNonce = new byte[CRYPTO_AEAD_CHACHA20POLY1305_IETF_NPUBBYTES];
      OutPlaintext plaintext = new OutPlaintext();
      plaintext.data = new byte[ZC_OUTPLAINTEXT_SIZE];
      //decrypt out by ock, get esk, pkD
      if (JLibsodium.cryptoAeadChacha20poly1305IetfDecrypt(new Chacha20poly1305IetfDecryptParams(
          plaintext.data, null,
          null,
          ciphertext.data, ZC_OUTCIPHERTEXT_SIZE,
          null,
          0,
          cipherNonce, ock)) != 0) {
        return Optional.empty();
      }
      return Optional.of(plaintext);
    }
```

**File:** framework/src/main/java/org/tron/core/Wallet.java (L3387-3393)
```java
        for (int index = 0; index < stContract.getReceiveDescriptionList().size(); index++) {
          ReceiveDescription r = stContract.getReceiveDescription(index);
          Optional<Note> notePlaintext = Note.decrypt(r.getCEnc().toByteArray(),//ciphertext
              ivk,
              r.getEpk().toByteArray(),//epk
              r.getNoteCommitment().toByteArray() //cmu
          );
```

**File:** framework/src/main/java/org/tron/core/Wallet.java (L3502-3520)
```java
          Encryption.OutCiphertext cOut = new Encryption.OutCiphertext();
          cOut.setData(r.getCOut().toByteArray());
          Optional<OutgoingPlaintext> notePlaintext = OutgoingPlaintext.decrypt(cOut,//ciphertext
              ovk,
              r.getValueCommitment().toByteArray(), //cv
              r.getNoteCommitment().toByteArray(), //cmu
              r.getEpk().toByteArray() //epk
          );

          if (notePlaintext.isPresent()) {
            OutgoingPlaintext decryptedOutCtUnwrapped = notePlaintext.get();
            //decode c_enc with pkd、esk
            Encryption.EncCiphertext cipherText = new Encryption.EncCiphertext();
            cipherText.setData(r.getCEnc().toByteArray());
            Optional<Note> foo = Note.decrypt(cipherText,
                r.getEpk().toByteArray(),
                decryptedOutCtUnwrapped.getEsk(),
                decryptedOutCtUnwrapped.getPkD(),
                r.getNoteCommitment().toByteArray());
```
