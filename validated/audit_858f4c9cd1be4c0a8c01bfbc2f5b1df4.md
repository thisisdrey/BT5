### Title
Trailing/unknown protobuf bytes in HTTP-submitted transactions bypass the anti-malleability checks applied to P2P transactions - ([File: framework/src/main/java/org/tron/core/services/http/BroadcastHexServlet.java])

### Summary
java-tron already has an explicit defense against the "trailing-bytes" class of bug: for transactions/blocks arriving over the P2P layer, `TransactionMessage`, `TransactionsMessage`, and `BlockMessage` call `Message.compareBytes(originalBytes, reserializedBytes)` and `TransactionCapsule.validContractProto(...)`, which re-serialize the parsed protobuf and fail if the byte lengths differ (i.e. if unknown/extra bytes were present) [1](#0-0) [2](#0-1) . That check is implemented via a reflection trick that forces `CodedInputStream.shouldDiscardUnknownFields` and a byte-length comparison [3](#0-2) , plus `TransactionCapsule.validContractProto()` which unpacks the `Any` contract payload, re-serializes it, and compares bytes to reject any extra/trailing bytes stuffed inside the contract field [4](#0-3) .

However, the HTTP entry points that accept a fully-formed, already-signed `Transaction` protobuf directly from an anonymous client — `BroadcastHexServlet` and the gRPC/HTTP `Wallet.broadcastTransaction()` — never perform this check.

### Finding Description
`BroadcastHexServlet.doPost()` parses attacker-supplied hex directly with the plain protobuf parser and hands the resulting `Transaction` straight to `wallet.broadcastTransaction(transaction)`: [5](#0-4) 

`Wallet.broadcastTransaction()` performs signature-length, TAPOS, expiration, and duplicate checks, but at no point calls `TransactionCapsule.validContractProto()` or `Message.compareBytes()`: [6](#0-5) 

Because standard protobuf parsing (`Transaction.parseFrom`) silently preserves any bytes that happen to form well-formed but undeclared protobuf fields as an `UnknownFieldSet` rather than rejecting them, a client can append extra field(s) to the `Transaction` message or inside the `Any` contract payload's byte value without the parse failing. These "trailing" bytes are the direct analog of the Erigon bug: bytes beyond what the schema/spec requires are silently accepted instead of being rejected as required by strict decoding.

By contrast, the exact same bytes submitted through the P2P path (`TransactionMessage`/`TransactionsMessage`) would be rejected via `compareBytes`/`validContractProto` once `Message.isFilter()` is enabled [7](#0-6) , and blocks assembled from such transactions would similarly fail `BlockMessage`'s check on peers [8](#0-7) . This creates an inconsistency between what the local node (that received the tx via the HTTP API and pushed it to its own pending pool / potentially block production) accepts and what other nodes validating via P2P will accept.

### Impact Explanation
If a node operator (or an SR producing blocks) submits/accepts such a malformed transaction through the HTTP `broadcasthex`/`broadcasttransaction` endpoints, it can be pushed into that node's local pending pool and potentially packed into a block it produces, while other nodes that would receive the same raw transaction over P2P (with `allowProtoFilterNum`/`isFilter()` enabled) would reject it via `compareBytes`/`validContractProto`. This produces divergent transaction/block acceptance across the network — the same class of consensus-inconsistency risk described in the Erigon report, where trailing bytes are accepted by one code path and rejected by another.

### Likelihood Explanation
Reaching this requires only an unauthenticated HTTP/gRPC caller submitting a crafted transaction to `/wallet/broadcasttransaction` or `/wallet/broadcasthex` — no privileged access needed. The actual protobuf wire-format conditions needed to smuggle a semantically-inert-but-length-changing unknown field are non-trivial to construct compared to the trivial RLP trailing-byte case in the original report, and I could not fully verify (within available tool budget) whether `isFilter()`/`allowProtoFilterNum` is enabled by default on mainnet, nor trace whether transaction ID computation (`hash(rawData)`) or block-level hashes would actually differ as a result, which is necessary to confirm an actual chain split rather than just a local acceptance inconsistency.

### Recommendation
Apply the same `Message.compareBytes()` + `TransactionCapsule.validContractProto()` checks (independent of the `isFilter()` dynamic-property gate) to all transaction ingestion paths, including `Wallet.broadcastTransaction()`, `BroadcastHexServlet`, and `BroadcastServlet`, so that a transaction accepted locally is guaranteed to be byte-for-byte re-serializable and thus acceptable to every other node in the network regardless of P2P filter settings.

### Proof of Concept
Not independently reproduced. Conceptually: craft a `Transaction` protobuf byte sequence with one legitimate signed field set plus an appended low-numbered, well-formed-but-undeclared protobuf field/tag (making it parseable as an `UnknownFieldSet` entry rather than a decode error), submit it as hex to `/wallet/broadcasthex`; confirm it is accepted (`SUCCESS` code) without any `compareBytes`/`validContractProto` rejection, then confirm the equivalent bytes fed into `TransactionMessage(byte[])` (P2P path with filtering enabled) are rejected via `PROTOBUF_ERROR`.

### Citations

**File:** framework/src/main/java/org/tron/core/net/message/adv/TransactionMessage.java (L14-22)
```java
  public TransactionMessage(byte[] data) throws Exception {
    super(data);
    this.transactionCapsule = new TransactionCapsule(getCodedInputStream(data));
    this.type = MessageTypes.TRX.asByte();
    if (Message.isFilter()) {
      compareBytes(data, transactionCapsule.getInstance().toByteArray());
      transactionCapsule
          .validContractProto(transactionCapsule.getInstance().getRawData().getContract(0));
    }
```

**File:** framework/src/main/java/org/tron/core/net/message/adv/BlockMessage.java (L15-22)
```java
  public BlockMessage(byte[] data) throws Exception {
    super(data);
    this.type = MessageTypes.BLOCK.asByte();
    this.block = new BlockCapsule(getCodedInputStream(data));
    if (Message.isFilter()) {
      Message.compareBytes(data, block.getInstance().toByteArray());
      TransactionCapsule.validContractProto(block.getInstance().getTransactionsList());
    }
```

**File:** chainbase/src/main/java/org/tron/common/overlay/message/Message.java (L50-66)
```java
  public static void compareBytes(byte[] src, byte[] dest) throws P2pException {
    if (src.length != dest.length) {
      throw new P2pException(PROTOBUF_ERROR, PROTOBUF_ERROR.getDesc());
    }
  }

  public static CodedInputStream getCodedInputStream(byte[] data) {
    CodedInputStream codedInputStream = CodedInputStream.newInstance(data);
    if (isFilter()) {
      ReflectionUtils.setField(field, codedInputStream, true);
    }
    return codedInputStream;
  }

  public static boolean isFilter() {
    return dynamicPropertiesStore.getAllowProtoFilterNum() == 1;
  }
```

**File:** chainbase/src/main/java/org/tron/core/capsule/TransactionCapsule.java (L403-415)
```java
  public static void validContractProto(Transaction.Contract contract)
      throws InvalidProtocolBufferException, P2pException {
    Any contractParameter = contract.getParameter();
    Class clazz = TransactionFactory.getContract(contract.getType());
    if (clazz == null) {
      throw new P2pException(PROTOBUF_ERROR, PROTOBUF_ERROR.getDesc());
    }
    com.google.protobuf.Message src = contractParameter.unpack(clazz);
    com.google.protobuf.Message contractMessage = parse(clazz,
        Message.getCodedInputStream(src.toByteArray()));

    Message.compareBytes(src.toByteArray(), contractMessage.toByteArray());
  }
```

**File:** framework/src/main/java/org/tron/core/services/http/BroadcastHexServlet.java (L23-44)
```java
  protected void doPost(HttpServletRequest request, HttpServletResponse response) {
    try {
      String input = request.getReader().lines()
          .collect(Collectors.joining(System.lineSeparator()));
      String trx = JSONObject.parseObject(input).getString("transaction");
      Transaction transaction = Transaction.parseFrom(ByteArray.fromHexString(trx));
      TransactionCapsule transactionCapsule = new TransactionCapsule(transaction);
      String transactionID = ByteArray
          .toHexString(transactionCapsule.getTransactionId().getBytes());
      GrpcAPI.Return result = wallet.broadcastTransaction(transaction);
      JSONObject json = new JSONObject();
      json.put("result", result.getResult());
      json.put("code", result.getCode().toString());
      json.put("message", result.getMessage().toStringUtf8());
      json.put("transaction", JsonFormat.printToString(transaction, true));
      json.put("txid", transactionID);

      response.getWriter().println(json.toJSONString());
    } catch (Exception e) {
      Util.processError(e, response);
    }
  }
```

**File:** framework/src/main/java/org/tron/core/Wallet.java (L507-576)
```java
  public GrpcAPI.Return broadcastTransaction(Transaction signedTransaction) {
    GrpcAPI.Return.Builder builder = GrpcAPI.Return.newBuilder();
    TransactionCapsule trx = new TransactionCapsule(signedTransaction);
    trx.setTime(System.currentTimeMillis());
    Sha256Hash txID = trx.getTransactionId();
    try {
      for (ByteString sig : signedTransaction.getSignatureList()) {
        if (!SignUtils.isValidLength(sig.size())) {
          String info = "Signature size is " + sig.size();
          logger.warn("Broadcast transaction {} has failed, {}.", txID, info);
          return builder.setResult(false).setCode(response_code.SIGERROR)
              .setMessage(ByteString.copyFromUtf8("Validate signature error: " + info))
              .build();
        }
      }

      if (tronNetDelegate.isBlockUnsolidified()) {
        logger.warn("Broadcast transaction {} has failed, block unsolidified.", txID);
        return builder.setResult(false).setCode(response_code.BLOCK_UNSOLIDIFIED)
          .setMessage(ByteString.copyFromUtf8("Block unsolidified."))
          .build();
      }

      if (minEffectiveConnection != 0) {
        if (tronNetDelegate.getActivePeer().isEmpty()) {
          logger.warn("Broadcast transaction {} has failed, no connection.", txID);
          return builder.setResult(false).setCode(response_code.NO_CONNECTION)
              .setMessage(ByteString.copyFromUtf8("No connection."))
              .build();
        }

        int count = (int) tronNetDelegate.getActivePeer().stream()
            .filter(p -> !p.isNeedSyncFromUs() && !p.isNeedSyncFromPeer())
            .count();

        if (count < minEffectiveConnection) {
          String info = "Effective connection:" + count + " lt minEffectiveConnection:"
              + minEffectiveConnection;
          logger.warn("Broadcast transaction {} has failed. {}.", txID, info);
          return builder.setResult(false).setCode(response_code.NOT_ENOUGH_EFFECTIVE_CONNECTION)
              .setMessage(ByteString.copyFromUtf8(info))
              .build();
        }
      }

      if (dbManager.isTooManyPending()) {
        logger.warn("Broadcast transaction {} has failed, too many pending.", txID);
        return builder.setResult(false).setCode(response_code.SERVER_BUSY)
            .setMessage(ByteString.copyFromUtf8("Server busy.")).build();
      }

      if (trxCacheEnable) {
        if (dbManager.getTransactionIdCache().getIfPresent(txID) != null) {
          logger.warn("Broadcast transaction {} has failed, it already exists.", txID);
          return builder.setResult(false).setCode(response_code.DUP_TRANSACTION_ERROR)
              .setMessage(ByteString.copyFromUtf8("Transaction already exists.")).build();
        } else {
          dbManager.getTransactionIdCache().put(txID, true);
        }
      }

      if (chainBaseManager.getDynamicPropertiesStore().supportVM()) {
        trx.resetResult();
      }
      if (trx.getInstance().getRawData().getContractCount() == 0) {
        throw new ContractValidateException(ActuatorConstant.CONTRACT_NOT_EXIST);
      }
      trx.checkExpiration(chainBaseManager.getNextBlockSlotTime());
      dbManager.pushTransaction(trx);
      TransactionMessage message = new TransactionMessage(trx.getInstance().toByteArray());
```
