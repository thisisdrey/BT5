[1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

### Citations

**File:** framework/src/main/java/org/tron/core/capsule/utils/RLP.java (L612-617)
```java
  private static void verifyLength(int suppliedLength, int availableLength) {
    if (suppliedLength > availableLength) {
      throw new RuntimeException(String.format("Length parsed from RLP (%s bytes) is greater "
          + "than possible size of data (%s bytes)", suppliedLength, availableLength));
    }
  }
```

**File:** framework/src/main/java/org/tron/core/capsule/utils/RLP.java (L643-644)
```java
      // check that length is in payload bounds
      verifyLength(lenbytes, data.length - pos - 1 - lenlen);
```

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L399-430)
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

  private static byte[][] extractSigArray(DataWord[] words, int offset, byte[] data) {
    if (offset > words.length - 1) {
      return new byte[0][];
    }
    int len = words[offset].intValueSafe();
    byte[][] bytesArray = new byte[len][];
    for (int i = 0; i < len; i++) {
      int bytesOffset = words[offset + i + 1].intValueSafe() / WORD_SIZE;
      bytesArray[i] = extractBytes(data, (bytesOffset + offset + 2) * WORD_SIZE,
          SIG_LENGTH);
    }
    return bytesArray;
  }

  private static byte[] extractBytes(byte[] data, int offset, int len) {
    return Arrays.copyOfRange(data, offset, offset + len);
  }
```

**File:** actuator/src/main/java/org/tron/core/vm/program/Memory.java (L31-58)
```java
  public byte[] read(int address, int size) {
    if (size <= 0) {
      return EMPTY_BYTE_ARRAY;
    }

    extend(address, size);
    byte[] data = new byte[size];

    int chunkIndex = address / CHUNK_SIZE;
    int chunkOffset = address % CHUNK_SIZE;

    int toGrab = data.length;
    int start = 0;

    while (toGrab > 0) {
      int copied = grabMax(chunkIndex, chunkOffset, toGrab, data, start);

      // read next chunk from the start
      ++chunkIndex;
      chunkOffset = 0;

      // mark remind
      toGrab -= copied;
      start += copied;
    }

    return data;
  }
```
