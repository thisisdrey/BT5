### Title
Uninitialized field due to self-assignment in `ReceiveDescriptionCapsule` constructor causes null-object dereference in shielded transaction building - (File: `framework/src/main/java/org/tron/core/capsule/ReceiveDescriptionCapsule.java`)

### Summary
The constructor `ReceiveDescriptionCapsule(final ReceiveDescription outputDescription)` contains a self-assignment bug: it assigns the (still-null) field `receiveDescription` to itself instead of assigning the constructor parameter `outputDescription`. [1](#0-0)  This is structurally analogous to the Open Babel GRO-parser flaw: a pointer/reference field that is expected to be initialized from caller-supplied data is instead left uninitialized (`null`), and every subsequent accessor on the object dereferences it.

### Finding Description
`ReceiveDescriptionCapsule` wraps a protobuf `ReceiveDescription` message that is populated from a caller-supplied object in one of its constructors. In the single-argument constructor, the assignment reads `this.receiveDescription = receiveDescription;` — the right-hand side refers to the (uninitialized) instance field rather than the parameter `outputDescription` [1](#0-0) . As a result, any `ReceiveDescriptionCapsule` built via this constructor holds `receiveDescription == null` regardless of the input passed in. Every accessor of the class (`getValueCommitment()`, `getEphemeralKey()`, `getEncCiphertext()`, `getOutCiphertext()`, `getCm()`, `getZkproof()`, `getData()`, `getInstance()`) directly dereferences `this.receiveDescription` without a null check [2](#0-1) , so any use of an instance constructed this way throws an unguarded `NullPointerException`.

`ReceiveDescriptionCapsule` is referenced extensively by the shielded-transaction builder code paths (`ZenTransactionBuilder.java` and `ShieldedTRC20ParametersBuilder.java`), which are used to assemble `ShieldedTransferContract`/shielded TRC20 parameters from client-supplied transaction request data via the Wallet API surface that handles shielded transaction creation. I located references to `ReceiveDescriptionCapsule` in both of these production classes but was not able to confirm, within the available tool iterations, which specific overload each call site invokes (the correctly implemented 6-argument constructor vs. the buggy single-argument constructor). This is the one point I could not fully verify.

### Impact Explanation
If the buggy single-argument constructor is reached from any code path that processes untrusted/attacker-controlled shielded transaction parameters (e.g., an API request that constructs a `ReceiveDescription` and passes it into `ReceiveDescriptionCapsule`), the resulting capsule silently loses all note-commitment/ciphertext/proof data and throws an NPE the moment any getter is invoked (including `getInstance()`/`getData()`, which are used for serialization). Depending on where in the call stack this exception propagates, this could crash the transaction-building/serving thread, causing that shielded-transaction API call to fail deterministically for any legitimate input that traverses this path — matching the "API the node can no longer serve" impact bar. It does not, by itself, appear to lead to consensus corruption, theft, or a chain split, since the affected object is discarded before being persisted to the actual `Protocol.Transaction` (which is separately built from the underlying protobuf builders).

### Likelihood Explanation
This is a real, exact-match code defect (self-assignment on an uninitialized field, exactly the CWE-824 "use of uninitialized pointer/reference" class cited in the external report). However, likelihood of actual exploitation depends entirely on whether the buggy overload is invoked anywhere in the reachable production call graph. I confirmed the class is referenced by `ZenTransactionBuilder.java` and `ShieldedTRC20ParametersBuilder.java`, both of which participate in constructing shielded transactions from client-supplied data, but I could not confirm within this session which constructor overload those call sites actually use. If they use the fully-parameterized 6-argument constructor (which is correctly implemented) instead of the single-argument one, this bug is currently dead code with no runtime impact.

### Recommendation
Fix the constructor to assign the parameter instead of the field to itself:
```java
public ReceiveDescriptionCapsule(final ReceiveDescription outputDescription) {
  this.receiveDescription = outputDescription;
}
```
Additionally, audit all call sites of `ReceiveDescriptionCapsule` in `ZenTransactionBuilder.java` and `ShieldedTRC20ParametersBuilder.java` to confirm whether this constructor overload is used, and add defensive null checks/guards in the getters as a defense-in-depth measure.

### Proof of Concept
```java
ReceiveDescription rd = ReceiveDescription.newBuilder()
    .setNoteCommitment(ByteString.copyFrom(new byte[32]))
    .build();
ReceiveDescriptionCapsule capsule = new ReceiveDescriptionCapsule(rd);
capsule.getCm(); // throws NullPointerException because this.receiveDescription is null
``` [1](#0-0)

### Citations

**File:** framework/src/main/java/org/tron/core/capsule/ReceiveDescriptionCapsule.java (L17-19)
```java
  public ReceiveDescriptionCapsule(final ReceiveDescription outputDescription) {
    this.receiveDescription = receiveDescription;
  }
```

**File:** framework/src/main/java/org/tron/core/capsule/ReceiveDescriptionCapsule.java (L47-133)
```java
  public ByteString getValueCommitment() {
    return this.receiveDescription.getValueCommitment();
  }

  public void setValueCommitment(byte[] bytes) {
    this.receiveDescription =
        this.receiveDescription.toBuilder().setValueCommitment(ByteString.copyFrom(bytes)).build();
  }

  public void setValueCommitment(ByteString bytes) {
    this.receiveDescription = this.receiveDescription.toBuilder().setValueCommitment(bytes).build();
  }

  public ByteString getEphemeralKey() {
    return this.receiveDescription.getEpk();
  }

  public void setEpk(byte[] bytes) {
    this.receiveDescription =
        this.receiveDescription.toBuilder().setEpk(ByteString.copyFrom(bytes)).build();
  }

  public void setEpk(ByteString bytes) {
    this.receiveDescription = this.receiveDescription.toBuilder().setEpk(bytes).build();
  }

  public ByteString getEncCiphertext() {
    return this.receiveDescription.getCEnc();
  }

  public void setCEnc(byte[] bytes) {
    this.receiveDescription =
        this.receiveDescription.toBuilder().setCEnc(ByteString.copyFrom(bytes)).build();
  }

  public void setCEnc(ByteString bytes) {
    this.receiveDescription = this.receiveDescription.toBuilder().setCEnc(bytes).build();
  }

  public ByteString getOutCiphertext() {
    return this.receiveDescription.getCOut();
  }

  public void setCOut(byte[] bytes) {
    this.receiveDescription =
        this.receiveDescription.toBuilder().setCOut(ByteString.copyFrom(bytes)).build();
  }

  public void setCOut(ByteString bytes) {
    this.receiveDescription = this.receiveDescription.toBuilder().setCOut(bytes).build();
  }

  public ByteString getCm() {
    return this.receiveDescription.getNoteCommitment();
  }

  public void setNoteCommitment(byte[] bytes) {
    this.receiveDescription =
        this.receiveDescription.toBuilder().setNoteCommitment(ByteString.copyFrom(bytes)).build();
  }

  public void setNoteCommitment(ByteString bytes) {
    this.receiveDescription = this.receiveDescription.toBuilder().setNoteCommitment(bytes).build();
  }

  public ByteString getZkproof() {
    return this.receiveDescription.getZkproof();
  }

  public void setZkproof(byte[] proof) {
    ByteString proof1 = ByteString.copyFrom(proof);
    this.receiveDescription = this.receiveDescription.toBuilder().setZkproof(proof1).build();
  }

  public void setZkproof(ByteString proof) {
    this.receiveDescription = this.receiveDescription.toBuilder().setZkproof(proof).build();
  }

  @Override
  public byte[] getData() {
    return this.receiveDescription.toByteArray();
  }

  @Override
  public ReceiveDescription getInstance() {
    return this.receiveDescription;
  }
```
