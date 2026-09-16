### Title
`TransactionUtil.validUrl` Permits Unicode BiDi Override Characters in On-Chain URL Metadata, Enabling Visual URL Spoofing in Wallets/Explorers - (File: `actuator/src/main/java/org/tron/core/utils/TransactionUtil.java`)

### Summary
`TransactionUtil.validUrl()` only enforces a byte-length bound and never restricts the character set of the `url` bytes it accepts. As a result, any account issuing a TRC10 asset, updating an asset, or registering/updating a witness (super representative candidate) can embed Unicode explicit-direction BiDi formatting characters (U+202A–U+202E, U+2066–U+2069) inside the on-chain `url` field. These bytes are stored verbatim and later returned unmodified by the HTTP/gRPC query APIs (`Wallet`, `TronJsonRpcImpl`) that any anonymous client can call, then rendered as-is by block explorers/wallet UIs, letting an attacker visually reorder or disguise the displayed URL text — the same bug class as GHSA-h5vq-qfcg-4m6p.

### Finding Description
`validUrl` delegates to `validBytes`, which only checks `bytes.length <= maxLength` and permits any byte content (unlike `validAssetName`/`validTokenAbbrName`, which call `validReadableBytes` and restrict every byte to the printable ASCII range `0x21`–`0x7E`): [1](#0-0) 

This `validUrl` check is the sole guard on the `url` field for:
- `AssetIssueContract.url` in `AssetIssueActuator.validate()` [2](#0-1) 
- `UpdateAssetContract.url` in `UpdateAssetActuator.validate()` [3](#0-2) 
- `WitnessCreateContract.url` and `WitnessUpdateContract.update_url` (same `TransactionUtil.validUrl` pattern is exercised by `WitnessCreateActuatorTest`/`WitnessUpdateActuatorTest`) [4](#0-3) 

None of these validators reject BiDi override code points, so a transaction can freely embed U+202E (RIGHT-TO-LEFT OVERRIDE) or similar characters inside a token's or witness's `url`.

Once stored, the value is serialized back to JSON by `JsonFormat.escapeText`, which only escapes ASCII control characters (`0x00`–`0x1F`), backslash, and double-quote — any other character, including BiDi formatting characters, is "printed as-is": [5](#0-4) 

Consequently every HTTP/gRPC endpoint that returns `AssetIssueContract`, `Witness`, or transaction contract data (e.g. `GetAssetIssueByName`, `GetAssetIssueList`, `ListWitnesses`, `GetTransactionById`) hands the raw BiDi-laden `url` string straight to any anonymous API client, which is exactly the pass-through condition described in the Symfony advisory for `UrlSanitizer::parse()`.

### Impact Explanation
An attacker (any account willing to pay the asset-issue fee, or any account registering as a witness candidate) can craft a `url` whose visual rendering in a wallet, explorer, or dApp differs from its actual byte content — e.g., making a phishing/malicious domain display as a well-known, trusted domain. This is a visual-spoofing primitive (CWE-1007/CWE-451) that can be leveraged for phishing against holders/voters who trust the displayed asset or witness URL, a step toward inducing users into transferring funds or votes based on a spoofed destination. It does not by itself corrupt consensus, crash nodes, or leak keys, so the impact is consistent with the Medium severity of the original advisory.

### Likelihood Explanation
Trivial to trigger: any funded account can submit an `AssetIssueContract`, `UpdateAssetContract`, `WitnessCreateContract`, or `WitnessUpdateContract` transaction with a crafted `url` containing BiDi override characters, since `validUrl`/`validBytes` impose no character-set restriction — only a length check.

### Recommendation
Reject or strip Unicode explicit-direction BiDi formatting characters (U+202A–U+202E, U+2066–U+2069) in `TransactionUtil.validUrl` (and ideally `validAssetDescription`) before accepting `AssetIssueContract.url`, `UpdateAssetContract.url`, `WitnessCreateContract.url`, and `WitnessUpdateContract.update_url`, mirroring the approach used by the patched Symfony `UrlSanitizer::parse()`.

### Proof of Concept
1. Build an `AssetIssueContract` (or `WitnessCreateContract`) whose `url` bytes are `"good-domain.com/" + U+202E + "moc.live-hsihp"` (RLO reverses the trailing segment when rendered).
2. Submit via `validate()`/`execute()` — `TransactionUtil.validUrl` only checks length (`<= 256`), so the transaction is accepted and stored on-chain.
3. Query the asset/witness via the HTTP API (`GetAssetIssueByName`/`ListWitnesses`) or gRPC `Wallet` service — the returned JSON contains the raw BiDi bytes unescaped, per `JsonFormat.escapeText`'s pass-through of non-control characters.
4. A wallet/explorer rendering this string in a bidi-aware UI (browser) will visually reorder the string, displaying a URL different from its actual bytes, spoofing the link a user sees before interacting with the asset or voting for the witness.

### Citations

**File:** actuator/src/main/java/org/tron/core/utils/TransactionUtil.java (L81-102)
```java
  public static boolean validUrl(byte[] url) {
    return validBytes(url, MAX_URL_LEN, false);
  }

  public static boolean validAccountId(byte[] accountId) {
    return validReadableBytes(accountId, MAX_ACCOUNT_ID_LEN) && accountId.length >= MIN_ACCOUNT_ID_LEN;
  }

  public static boolean validAssetName(byte[] assetName) {
    return validReadableBytes(assetName, MAX_ASSET_NAME_LEN);
  }

  public static boolean validTokenAbbrName(byte[] abbrName) {
    return validReadableBytes(abbrName, MAX_TOKEN_ABBR_NAME_LEN);
  }

  private static boolean validBytes(byte[] bytes, int maxLength, boolean allowEmpty) {
    if (ArrayUtils.isEmpty(bytes)) {
      return allowEmpty;
    }
    return bytes.length <= maxLength;
  }
```

**File:** actuator/src/main/java/org/tron/core/actuator/AssetIssueActuator.java (L188-190)
```java
    if (!TransactionUtil.validUrl(assetIssueContract.getUrl().toByteArray())) {
      throw new ContractValidateException("Invalid url");
    }
```

**File:** actuator/src/main/java/org/tron/core/actuator/UpdateAssetActuator.java (L151-153)
```java
    if (!TransactionUtil.validUrl(newUrl.toByteArray())) {
      throw new ContractValidateException("Invalid url");
    }
```

**File:** framework/src/test/java/org/tron/core/actuator/WitnessCreateActuatorTest.java (L172-208)
```java
  @Test
  public void InvalidUrlTest() {
    TransactionResultCapsule ret = new TransactionResultCapsule();
    //Url cannot empty
    try {
      WitnessCreateActuator actuator = new WitnessCreateActuator();
      actuator.setChainBaseManager(dbManager.getChainBaseManager())
          .setAny(getContract(OWNER_ADDRESS_FIRST, ByteString.EMPTY));
      actuator.validate();
      actuator.execute(ret);
      fail("Invalid url");
    } catch (ContractValidateException e) {
      Assert.assertTrue(e instanceof ContractValidateException);
      Assert.assertEquals("Invalid url", e.getMessage());
    } catch (ContractExeException e) {
      Assert.assertFalse(e instanceof ContractExeException);
    }

    //256 bytes
    String url256Bytes = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef012345678"
        + "9abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0"
        + "123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef01234567"
        + "89abcdef";
    //Url length can not greater than 256
    try {
      WitnessCreateActuator actuator = new WitnessCreateActuator();
      actuator.setChainBaseManager(dbManager.getChainBaseManager())
          .setAny(getContract(OWNER_ADDRESS_FIRST, ByteString.copyFromUtf8(url256Bytes + "0")));
      actuator.validate();
      actuator.execute(ret);
      fail("Invalid url");
    } catch (ContractValidateException e) {
      Assert.assertTrue(e instanceof ContractValidateException);
      Assert.assertEquals("Invalid url", e.getMessage());
    } catch (ContractExeException e) {
      Assert.assertFalse(e instanceof ContractExeException);
    }
```

**File:** framework/src/main/java/org/tron/core/services/http/JsonFormat.java (L934-982)
```java
  static String escapeText(String input) {
    StringBuilder builder = new StringBuilder(input.length());
    CharacterIterator iter = new StringCharacterIterator(input);
    for (char c = iter.first(); c != CharacterIterator.DONE; c = iter.next()) {
      switch (c) {
        case '\b':
          builder.append("\\b");
          break;
        case '\f':
          builder.append("\\f");
          break;
        case '\n':
          builder.append("\\n");
          break;
        case '\r':
          builder.append("\\r");
          break;
        case '\t':
          builder.append("\\t");
          break;
        case '\\':
          builder.append("\\\\");
          break;
        case '"':
          builder.append("\\\"");
          break;
        default:
          // Check for other control characters
          if (c >= 0x0000 && c <= 0x001F) {
            appendEscapedUnicode(builder, c);
          } else if (Character.isHighSurrogate(c)) {
            // Encode the surrogate pair using 2 six-character sequence (\\uXXXX\\uXXXX)
            appendEscapedUnicode(builder, c);
            c = iter.next();
            if (c == CharacterIterator.DONE) {
              throw new IllegalArgumentException(
                  "invalid unicode string: unexpected high surrogate pair value "
                      + "without corresponding low value.");
            }
            appendEscapedUnicode(builder, c);
          } else {
            // Anything else can be printed as-is
            builder.append(c);
          }
          break;
      }
    }
    return builder.toString();
  }
```
