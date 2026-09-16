No Vulnerability found for this question.

CVE-2022-21628 concerns Oracle's built-in `com.sun.net.httpserver` "Lightweight HTTP Server" used for client-side Java Web Start/applet deployments running untrusted code, and the advisory itself states it does not apply to server deployments running trusted code. java-tron's HTTP/JSON-RPC API surfaces are built entirely on the Eclipse Jetty stack (`jetty-server`, `jetty-servlet`), not `com.sun.net.httpserver`, as seen in [1](#0-0)  and the dependency declaration [2](#0-1) . All API services (`FullNodeHttpApiService`, `FullNodeJsonRpcHttpService`, `JsonRpcServiceOnSolidity`, `JsonRpcServiceOnPBFT`) extend this Jetty-based `HttpService` [3](#0-2) , so there is no code path in java-tron that reuses or reimplements the specific vulnerable JDK component. No reachable analog exists under the given scope rules.

### Citations

**File:** framework/src/main/java/org/tron/common/application/HttpService.java (L27-35)
```java
import org.eclipse.jetty.http.HttpStatus;
import org.eclipse.jetty.server.ConnectionLimit;
import org.eclipse.jetty.server.Request;
import org.eclipse.jetty.server.Server;
import org.eclipse.jetty.server.handler.ErrorHandler;
import org.eclipse.jetty.server.handler.SizeLimitHandler;
import org.eclipse.jetty.servlet.ServletContextHandler;
import org.eclipse.jetty.util.BufferUtil;
import org.tron.core.config.args.Args;
```

**File:** framework/build.gradle (L47-50)
```gradle
    // http
    implementation 'org.eclipse.jetty:jetty-server:9.4.58.v20250814'
    implementation 'org.eclipse.jetty:jetty-servlet:9.4.58.v20250814'
    // end http
```

**File:** framework/src/main/java/org/tron/core/services/jsonrpc/FullNodeJsonRpcHttpService.java (L18-28)
```java
public class FullNodeJsonRpcHttpService extends HttpService {

  @Autowired
  private JsonRpcServlet jsonRpcServlet;

  public FullNodeJsonRpcHttpService() {
    port = Args.getInstance().getJsonRpcHttpFullNodePort();
    enable = isFullNode() && Args.getInstance().isJsonRpcHttpFullNodeEnable();
    contextPath = "/";
    maxRequestSize = Args.getInstance().getJsonRpcMaxMessageSize();
  }
```
