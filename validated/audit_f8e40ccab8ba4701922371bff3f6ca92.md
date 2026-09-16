[1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

### Citations

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1433-1442)
```java
      } catch (Throwable any) {
        String errorMsg = any.getMessage();
        if (errorMsg == null && any.getCause() != null) {
          errorMsg = any.getCause().getMessage();
        }
        logger.info("VerifyMintProof exception " + errorMsg);
      } finally {
        JLibrustzcash.librustzcashSaplingVerificationCtxFree(ctx);
      }
      return Pair.of(true, DataWord.ZERO().getData());
```

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1476-1494)
```java
        //parse unfixed field offset
        int spendOffset = parseInt(data, 0);
        int spendAuthSigOffset = parseInt(data, 32);
        int receiveOffset = parseInt(data, 64);
        System.arraycopy(data, 96, bindingSig, 0, 64);
        System.arraycopy(data, 160, signHash, 0, 32);
        //parse value
        long value = parseLong(data, 192);
        for (int i = 0; i < 33; i++) {
          System.arraycopy(data, i * 32 + 224, frontier[i], 0, 32);
        }
        long leafCount = parseLong(data, 1280);
        if (leafCount >= TREE_WIDTH - 1) {
          return Pair.of(true, DataWord.ZERO().getData());
        }

        int spendCount = parseInt(data, spendOffset);
        int spendAuthSigCount = parseInt(data, spendAuthSigOffset);
        int receiveCount = parseInt(data, receiveOffset);
```

**File:** framework/src/main/java/org/tron/common/logsfilter/ContractEventParser.java (L80-90)
```java
  protected static byte[] subBytes(byte[] src, int start, int length) {
    if (ArrayUtils.isEmpty(src)) {
      throw new OutputLengthException("source data is empty");
    }
    if (start < 0 || start >= src.length || length < 0 || length > src.length - start) {
      throw new OutputLengthException(
          "data start:" + start + ", length:" + length + ", src.length:" + src.length);
    }
    byte[] dst = new byte[length];
    System.arraycopy(src, start, dst, 0, length);
    return dst;
```

**File:** framework/src/main/java/org/tron/core/capsule/utils/RLP.java (L612-617)
```java
  private static void verifyLength(int suppliedLength, int availableLength) {
    if (suppliedLength > availableLength) {
      throw new RuntimeException(String.format("Length parsed from RLP (%s bytes) is greater "
          + "than possible size of data (%s bytes)", suppliedLength, availableLength));
    }
  }
```

**File:** framework/src/main/java/org/tron/core/capsule/utils/RLP.java (L718-721)
```java
  private static DecodeResult decodeList(byte[] data, int pos, int len) {
    // check that length is in payload bounds
    verifyLength(len, data.length - pos);
    int prevPos;
```
