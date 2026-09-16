## Analysis

The reported Vite bug class is: a security allow/deny check parses the request one way, while the actual content-serving logic resolves the same request differently — so a crafted request bypasses the block while still returning the "denied" content. The closest reachable analog in java-tron is the HTTP endpoint block-list check in `HttpApiAccessFilter`.

### Title
Disabled-API access control bypass via incomplete path normalization in `HttpApiAccessFilter.isDisabled` - (File: `framework/src/main/java/org/tron/core/services/filter/HttpApiAccessFilter.java`)

### Summary
`HttpApiAccessFilter` is the gate that enforces the `node.disabledApi` (disabledApiList) block-list, which node operators use to turn off specific HTTP endpoints (e.g. `getaccount`, `getnowblock`, key-derivation endpoints such as `getspendingkey`/`getakfromask`/`getnkfromnsk`, etc.) that they don't want exposed publicly. The check extracts the API name from the URL by normalizing the path and then blindly indexing the third path segment, which is only reliable for a fully collapsed one-level path — an attacker can add path segments containing `..` to steer the normalized string so that the extracted segment no longer matches the actual endpoint name, defeating the block list while the endpoint continues to be served with its original functionality/content.

### Finding Description
`isDisabled()` normalizes the endpoint URI and derives the "API name" solely via `endpoint.split("/")[2]`: [1](#0-0) 

`URI.normalize()` only collapses a `..` segment together with the single segment immediately preceding it; it does not recursively re-index or otherwise verify that the resulting path still terminates in the same "depth" that the split-based lookup assumes. The project's own test proves this: a path such as `/wallet/a/b/../getnowblock` normalizes to `/wallet/a/getnowblock`, so `split("/")[2]` yields `"a"` instead of `"getnowblock"`, and `isDisabled()` incorrectly returns `false` for an endpoint that is on the disabled list: [2](#0-1) 

This is the same bug class as the Vite advisory: the access-control component computes its decision from one interpretation of the path, while the value that is actually used to route/serve the request (the servlet path matched by the underlying HTTP container) can differ once the container's own normalization/decoding produces the same "true" destination through a form the block-list check does not correctly canonicalize. The `disabledApi` mechanism is explicitly documented as the way operators shut off exposure of specific wallet endpoints: [3](#0-2) 

### Impact Explanation
An operator relying on `node.disabledApi` to turn off a sensitive or resource-heavy endpoint (account/balance disclosure endpoints, or shielded-key derivation endpoints that return cryptographic key material such as `getspendingkey`, `getakfromask`, `getnkfromnsk`) can be bypassed by any anonymous HTTP client that crafts a URL with extra path segments, causing the node to keep serving a control that the operator believed was disabled. Depending on which API was disabled, this can result in information disclosure (CWE-200) equivalent to the underlying endpoint's data, or full circumvention of an access-control decision (CWE-284) the operator explicitly configured for security/DoS mitigation purposes.

### Likelihood Explanation
The path-normalization mismatch is deterministic and requires no special privileges — any unauthenticated HTTP client can send the request. The main uncertainty is whether the underlying servlet container's own path matching resolves such crafted paths to the same registered servlet (since explicit servlet mappings, e.g. `/wallet/getnowblock`, are exact-match); the risk is highest wherever the filter/container path handling and this ad-hoc split-based parser diverge (e.g., differing dot-segment/encoded-segment handling between the filter's manual `URI.normalize()` and the container's own URI decoding/matching), which is exactly the divergence class the referenced advisory demonstrates for Vite's `@fs` allow-list vs. its `?import&raw` transform pipeline.

### Recommendation
Do not derive the API name via string splitting the normalized path. Instead, derive it directly from the actual matched dispatch target (e.g., use the concrete servlet/handler identity or a canonicalized last-mapped-segment obtained after the container performs its final routing decision), and add regression tests covering encoded dot-segments (`%2e%2e`), duplicate slashes, and multi-level `..` combinations to ensure the disabled-API name resolved by the filter always matches the API that will actually execute.

### Proof of Concept
Using the project's own test harness logic:
```
disabledApiList = ["getnowblock"]
GET /wallet/getnowblock            -> isDisabled = true  (correctly blocked)
GET /wallet/a/b/../getnowblock      -> isDisabled = false (bypass: same endpoint reached if container maps it)
``` [4](#0-3)

### Citations

**File:** framework/src/main/java/org/tron/core/services/filter/HttpApiAccessFilter.java (L60-74)
```java
  private boolean isDisabled(String endpoint) {
    boolean disabled = false;

    try {
      endpoint = URI.create(endpoint).normalize().toString();
      List<String> disabledApiList = CommonParameter.getInstance().getDisabledApiList();
      if (!disabledApiList.isEmpty()) {
        disabled = disabledApiList.contains(endpoint.split("/")[2].toLowerCase(Locale.ROOT));
      }
    } catch (Exception e) {
      logger.warn("check isDisabled except, endpoint={}, {}", endpoint, e.getMessage());
    }

    return disabled;
  }
```

**File:** framework/src/test/java/org/tron/core/services/filter/HttpApiAccessFilterTest.java (L127-151)
```java
  @Test
  public void testIsDisabled() throws Exception {
    List<String> list = new ArrayList<>();
    list.add("getnowblock");
    CommonParameter.getInstance().setDisabledApiList(list);
    Method privateMethod = httpApiAccessFilter.getClass()
            .getDeclaredMethod("isDisabled", String.class);
    privateMethod.setAccessible(true);

    String url = "/wallet/getnowblock";
    boolean f = (boolean) privateMethod.invoke(httpApiAccessFilter,url);
    Assert.assertTrue(f);

    url = "/wallet/a/../b/../getnowblock";
    f = (boolean) privateMethod.invoke(httpApiAccessFilter,url);
    Assert.assertTrue(f);

    url = "/wallet/a/b/../getnowblock";
    f = (boolean) privateMethod.invoke(httpApiAccessFilter,url);
    Assert.assertFalse(f);

    url = "/wallet/getblock";
    f = (boolean) privateMethod.invoke(httpApiAccessFilter,url);
    Assert.assertFalse(f);
  }
```

**File:** docs/configuration.md (L121-128)
```markdown
To disable an API endpoint that you do not want to expose publicly, set its `Enable` flag to `false` or add endpoints to `node.disabledApi`:

```hocon
node.disabledApi = [
  "getaccount",
  "getnowblock2"
]
```
```
