## Analysis

The reported bug class (insufficient validation of a URL field that permits a `javascript:` scheme, later rendered by a client and enabling XSS) has a direct analog in java-tron's on-chain metadata URL fields.

### Root cause

`TransactionUtil.validUrl` only checks byte length — it does **not** validate URL scheme or content at all: [1](#0-0) 

This same `validUrl` check is the sole gate used by the asset-issuance actuators, both reachable by any unprivileged account (an "asset issuer"):

- `AssetIssueActuator.validate()` validates `AssetIssueContract.url` with only `TransactionUtil.validUrl(...)`: [2](#0-1) 
- `UpdateAssetActuator.validate()` validates `UpdateAssetContract.url` the same way, and any account that has issued a token can call this at will: [3](#0-2) 

Once accepted, the raw attacker-controlled string is persisted unmodified into the `AssetIssueCapsule` and exposed via `Wallet`/HTTP/gRPC asset-issue query APIs (e.g. `getassetissuebyname`, `getassetissuelist`), to be consumed by any client (wallet UI, block explorer) that displays or links the `url` field — exactly the "stored payload → later rendered by a victim" pattern described in the CVE.

Note that the JSON output escaping added around this field (`JsonFormatEscapeTest`, `HttpSelfFormatFieldName`) only guards against JSON/control-character injection, not the URL *scheme* itself: [4](#0-3) [5](#0-4) . A properly-escaped JSON string `"javascript:alert(1)"` is still returned as-is to any downstream consumer, and any UI (wallet, explorer) that treats this field as a clickable/openable link is vulnerable to stored XSS/URI-scheme abuse when a victim clicks it.

(The equivalent witness `url`/`update_url` fields in `WitnessCreateActuator`/`WitnessUpdateActuator` have the identical flaw, but per the scan rules witness-related analogs are out of scope; the asset-issuance path is in scope as it is reachable by any ordinary "asset issuer.")

### Title
Insufficient URL validation in AssetIssueContract/UpdateAssetContract allows storing `javascript:`-scheme URLs, enabling stored XSS in downstream consumers - (File: `actuator/src/main/java/org/tron/core/utils/TransactionUtil.java`)

### Summary
`TransactionUtil.validUrl()` — the only validation applied to the `url` field of `AssetIssueContract` and `UpdateAssetContract` — checks nothing but byte length (≤256 bytes, non-empty). It does not restrict scheme or characters, so an attacker can set `url` to `javascript:alert(document.cookie)` or similar payloads that will be stored permanently on-chain in the `AssetIssueStore`/`AssetIssueV2Store` and served back through public query APIs.

### Finding Description
`AssetIssueActuator.validate()` and `UpdateAssetActuator.validate()` both gate the `url` field solely with `TransactionUtil.validUrl(bytes)`, which delegates to `validBytes(bytes, MAX_URL_LEN, false)` — a pure length/empty check with no scheme allow-list (e.g. no restriction to `http`/`https`). Any account can issue or update a token via a normal signed transaction and set an arbitrary string (including `javascript:` or `data:text/html,...`) as the asset's `url`. This value is stored verbatim in the `AssetIssueCapsule` and returned unmodified by asset-issue query endpoints for consumption by wallets, explorers, or any UI that renders the URL as a clickable link.

### Impact Explanation
Any client application (wallet, exchange dashboard, block explorer) that treats the on-chain `url` metadata field as a safe hyperlink and renders/opens it is exposed to script execution in the context of that page when a user clicks the malicious asset's "website" link — a stored XSS vector analogous to the reported CVE, potentially leading to session/token disclosure for users interacting with the affected UI.

### Likelihood Explanation
High likelihood of reachability: any account can issue a TRC10 token or update an existing one it owns via a standard `AssetIssueContract`/`UpdateAssetContract` transaction, requiring no special privileges beyond having a signed transaction accepted by the network (asset issuance fee only).

### Recommendation
Enforce a strict scheme allow-list (e.g. only `http://` and `https://`) and character sanitization in `TransactionUtil.validUrl`, rejecting `javascript:`, `data:`, `vbscript:`, and similar dangerous schemes, in addition to the existing length check.

### Proof of Concept
1. Attacker constructs an `AssetIssueContract` (or `UpdateAssetContract` for an owned asset) with `url = "javascript:alert(document.cookie)"`.
2. Broadcasts the signed transaction; `AssetIssueActuator.validate()`/`UpdateAssetActuator.validate()` accept it because `TransactionUtil.validUrl` only checks length.
3. The malicious URL is persisted in `AssetIssueStore`/`AssetIssueV2Store` and returned by asset-issue query APIs.
4. A victim using a wallet/explorer that links the asset's `url` field clicks it, triggering script execution in the victim's browser session.

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

**File:** actuator/src/main/java/org/tron/core/actuator/UpdateAssetActuator.java (L119-153)
```java
    ByteString newUrl = updateAssetContract.getUrl();
    ByteString newDescription = updateAssetContract.getDescription();

    if (!DecodeUtil.addressValid(ownerAddress)) {
      throw new ContractValidateException("Invalid ownerAddress");
    }

    AccountCapsule account = accountStore.get(ownerAddress);
    if (account == null) {
      throw new ContractValidateException("Account does not exist");
    }

    if (dynamicStore.getAllowSameTokenName() == 0) {
      if (account.getAssetIssuedName().isEmpty()) {
        throw new ContractValidateException("Account has not issued any asset");
      }

      if (assetIssueStore.get(account.getAssetIssuedName().toByteArray())
          == null) {
        throw new ContractValidateException("Asset is not existed in AssetIssueStore");
      }
    } else {
      if (account.getAssetIssuedID().isEmpty()) {
        throw new ContractValidateException("Account has not issued any asset");
      }

      if (assetIssueV2Store.get(account.getAssetIssuedID().toByteArray())
          == null) {
        throw new ContractValidateException("Asset is not existed in AssetIssueV2Store");
      }
    }

    if (!TransactionUtil.validUrl(newUrl.toByteArray())) {
      throw new ContractValidateException("Invalid url");
    }
```

**File:** framework/src/main/java/org/tron/core/services/http/HttpSelfFormatFieldName.java (L216-219)
```java
    NameFieldNameMap.put("protocol.AssetIssueContract.name", 1);
    NameFieldNameMap.put("protocol.AssetIssueContract.abbr", 1);
    NameFieldNameMap.put("protocol.AssetIssueContract.description", 1);
    NameFieldNameMap.put("protocol.AssetIssueContract.url", 1);
```

**File:** framework/src/test/java/org/tron/core/services/http/JsonFormatEscapeTest.java (L43-64)
```java
  private static final String URL_FIELD = "protocol.AssetIssueContract.url";
  private static final String DESC_FIELD = "protocol.AssetIssueContract.description";
  private static final String NAME_FIELD = "protocol.AssetIssueContract.name";
  private static final String ABBR_FIELD = "protocol.AssetIssueContract.abbr";
  private static final String CONTRACT_NAME_FIELD =
      "protocol.Transaction.Contract.ContractName";
  private static final String[] NAME_STRING_FIELDS = {
      "protocol.Return.message",
      "protocol.Address.host",
      "protocol.Note.memo",
      "protocol.AccountUpdateContract.account_name",
      "protocol.SetAccountIdContract.account_id",
      "protocol.TransferAssetContract.asset_name",
      "protocol.WitnessCreateContract.url",
      "protocol.WitnessUpdateContract.update_url",
      "protocol.AssetIssueContract.name",
      "protocol.AssetIssueContract.abbr",
      "protocol.AssetIssueContract.description",
      "protocol.AssetIssueContract.url",
      "protocol.ParticipateAssetIssueContract.asset_name",
      "protocol.UpdateAssetContract.url",
      "protocol.UpdateAssetContract.description",
```
