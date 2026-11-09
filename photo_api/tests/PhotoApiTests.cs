using System;
using System.Collections.Generic;
using System.IO;
using System.Net;
using System.Net.Sockets;
using System.Reflection;
using System.Security.AccessControl;
using System.Security.Principal;
using System.Text;
using System.Threading;
using EventDesk.PhotoApi;

internal static class PhotoApiTests
{
    // The suffix guarantees the service's minimum distinct-character count.
    private static readonly string Token = Guid.NewGuid().ToString("N") + "-fixture-alphabet";
    private static int assertions;

    private static int Main()
    {
        try
        {
            TestIdentifiersAndConfiguration();
            TestConfigurationDirectorySecurity();
            TestExistingConfigurationFileSecurity();
            TestHttpContract();
            TestConcurrencyAndShutdown();
            TestServiceShutdownCallback();
            TestLoopbackTransportPolicy();
            Console.WriteLine("PASS: " + assertions + " assertions; no SQL connections or external requests.");
            return 0;
        }
        catch (Exception error)
        {
            Console.Error.WriteLine("FAIL: " + error.GetType().Name + ": " + error.Message);
            return 1;
        }
    }

    private static void TestIdentifiersAndConfiguration()
    {
        string normalized;
        Check(EmployeeId.TryNormalize("123", out normalized) && normalized == "0000123", "Numeric IDs pad to seven");
        Check(EmployeeId.TryNormalize("Ab_12-xy", out normalized) && normalized == "Ab_12-xy", "ASCII identifiers preserved");
        Check(EmployeeId.TryNormalize("0000123", out normalized) && normalized == "0000123", "Canonical ID unchanged");
        Check(EmployeeId.TryNormalize(new string('9', 50), out normalized) && normalized.Length == 50, "Long numeric IDs are preserved without truncation");
        foreach (string invalid in new string[] { null, "", "../x", "a/b", "a b", "é", "１２３", new string('x', 51) })
            Check(!EmployeeId.TryNormalize(invalid, out normalized), "Invalid identifier rejected");

        SecretSettings settings = new SecretSettings();
        settings.ConnectionString = "Server=synthetic.invalid;Initial Catalog=Employee_db;User ID=test;Password=synthetic-only;Encrypt=True;TrustServerCertificate=True";
        settings.ApiToken = Token;
        settings.Port = 8765;
        SecretSettings validated = ConfigStore.Validate(settings);
        System.Data.SqlClient.SqlConnectionStringBuilder builder = new System.Data.SqlClient.SqlConnectionStringBuilder(validated.ConnectionString);
        Check(builder.Encrypt && builder.TrustServerCertificate, "Explicit encrypted self-signed pilot configuration retained");
        Check(builder.ConnectTimeout == 5 && builder.MaxPoolSize == 8 && builder.Pooling, "SQL resource limits enforced");
        Check(settings.ConnectionString != validated.ConnectionString, "Configuration input remains unchanged");
        ExpectInvalid(new SecretSettings { ConnectionString = settings.ConnectionString, ApiToken = "short", Port = 8765 });
        ExpectInvalid(new SecretSettings { ConnectionString = settings.ConnectionString, ApiToken = new string('a', 32), Port = 8765 });
        ExpectInvalid(new SecretSettings { ConnectionString = settings.ConnectionString, ApiToken = Token + "\r\n", Port = 8765 });
        ExpectInvalid(new SecretSettings { ConnectionString = settings.ConnectionString, ApiToken = Token, Port = 8766 });
        ExpectInvalid(new SecretSettings { ConnectionString = settings.ConnectionString.Replace("Encrypt=True", "Encrypt=False"), ApiToken = Token, Port = 8765 });
        ExpectInvalid(new SecretSettings { ConnectionString = settings.ConnectionString.Replace("Employee_db", "master"), ApiToken = Token, Port = 8765 });
    }

    private const string ServiceSid = "S-1-5-80-2322066080-1187967577-3785949985-1944469511-2490776966";

    private static void TestConfigurationDirectorySecurity()
    {
        MethodInfo validate = typeof(ConfigStore).GetMethod("ValidateConfigurationDirectorySecurity", BindingFlags.Static | BindingFlags.NonPublic);
        Check(validate != null, "Configuration directory has a pure ACL validator");
        DirectorySecurity valid = ConfigurationDirectoryAcl(ServiceSid);
        validate.Invoke(null, new object[] { valid });
        Check(valid.AreAccessRulesProtected, "Installer ACL with SYSTEM, Administrators and exact service SID is accepted");
        ExpectUnsafeAcl(validate, null, "Missing ACL is rejected");
        DirectorySecurity inherited = ConfigurationDirectoryAcl(ServiceSid);
        inherited.SetAccessRuleProtection(false, false);
        ExpectUnsafeAcl(validate, inherited, "Inherited directory permissions are rejected");
        DirectorySecurity empty = new DirectorySecurity();
        empty.SetAccessRuleProtection(true, false);
        empty.SetOwner(new SecurityIdentifier("S-1-5-32-544"));
        ExpectUnsafeAcl(validate, empty, "Empty protected ACL is rejected");
        foreach (string sid in new string[] { "S-1-1-0", "S-1-5-11", "S-1-5-32-545" })
        {
            foreach (FileSystemRights rights in new FileSystemRights[] { FileSystemRights.Read, FileSystemRights.Write, FileSystemRights.FullControl })
            {
                DirectorySecurity broad = ConfigurationDirectoryAcl(ServiceSid);
                broad.AddAccessRule(new FileSystemAccessRule(new SecurityIdentifier(sid), rights, AccessControlType.Allow));
                ExpectUnsafeAcl(validate, broad, "Additional read or write principal is rejected");
            }
        }
        ExpectUnsafeAcl(validate, ConfigurationDirectoryAcl("S-1-5-80-1-2-3-4-5"), "Another service SID is rejected");
        DirectorySecurity wrongOwner = ConfigurationDirectoryAcl(ServiceSid);
        wrongOwner.SetOwner(new SecurityIdentifier("S-1-1-0"));
        ExpectUnsafeAcl(validate, wrongOwner, "Unexpected directory owner is rejected");
        DirectorySecurity writableService = ConfigurationDirectoryAcl(ServiceSid);
        writableService.AddAccessRule(new FileSystemAccessRule(new SecurityIdentifier(ServiceSid), FileSystemRights.Write, AccessControlType.Allow));
        ExpectUnsafeAcl(validate, writableService, "Service cannot acquire configuration write access");
        DirectorySecurity missingSystem = ConfigurationDirectoryAcl(ServiceSid);
        missingSystem.RemoveAccessRuleAll(new FileSystemAccessRule(new SecurityIdentifier("S-1-5-18"), FileSystemRights.FullControl, AccessControlType.Allow));
        ExpectUnsafeAcl(validate, missingSystem, "Incomplete installer ACL is rejected");
        DirectorySecurity inheritedRule = new DirectorySecurity();
        inheritedRule.SetSecurityDescriptorSddlForm("O:BAG:BAD:P(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)(A;OICIID;0x1200a9;;;" + ServiceSid + ")");
        ExpectUnsafeAcl(validate, inheritedRule, "Protected ACL cannot retain inherited service grants");
        DirectorySecurity nonInheriting = ConfigurationDirectoryAcl(ServiceSid);
        nonInheriting.ResetAccessRule(new FileSystemAccessRule(new SecurityIdentifier(ServiceSid), FileSystemRights.ReadAndExecute, AccessControlType.Allow));
        ExpectUnsafeAcl(validate, nonInheriting, "Service permission must propagate to protected configuration files");
        DirectorySecurity denied = ConfigurationDirectoryAcl(ServiceSid);
        denied.AddAccessRule(new FileSystemAccessRule(new SecurityIdentifier(ServiceSid), FileSystemRights.Read, AccessControlType.Deny));
        ExpectUnsafeAcl(validate, denied, "Deny entries that differ from the installer contract are rejected");

        MethodInfo configure = typeof(ConfigStore).GetMethod("ConfigureFromStandardInput", BindingFlags.Static | BindingFlags.NonPublic,
            null, new Type[] { typeof(string), typeof(Func<byte[]>) }, null);
        Check(configure != null, "Configuration supports isolated directory and input reader tests");
        string directory = Path.Combine(Path.GetTempPath(), "EventDeskPhotoApi-acl-test-" + Guid.NewGuid().ToString("N"));
        ExpectDirectoryRejectedBeforeInput(configure, directory);
        Check(!Directory.Exists(directory), "Configuration cannot create a missing installation directory");
        Directory.CreateDirectory(directory);
        try
        {
            ExpectDirectoryRejectedBeforeInput(configure, directory);
            Check(Directory.GetFileSystemEntries(directory).Length == 0, "Unsafe directory receives no configuration or temporary files");
        }
        finally { Directory.Delete(directory); }
    }

    private static DirectorySecurity ConfigurationDirectoryAcl(string serviceSid)
    {
        DirectorySecurity security = new DirectorySecurity();
        security.SetAccessRuleProtection(true, false);
        security.SetOwner(new SecurityIdentifier("S-1-5-32-544"));
        foreach (string sid in new string[] { "S-1-5-18", "S-1-5-32-544", serviceSid })
        {
            FileSystemRights rights = sid == serviceSid ? FileSystemRights.ReadAndExecute : FileSystemRights.FullControl;
            security.AddAccessRule(new FileSystemAccessRule(new SecurityIdentifier(sid), rights,
                InheritanceFlags.ContainerInherit | InheritanceFlags.ObjectInherit, PropagationFlags.None, AccessControlType.Allow));
        }
        return security;
    }

    private static void ExpectUnsafeAcl(MethodInfo validate, FileSystemSecurity security, string message)
    {
        bool rejected = false;
        try { validate.Invoke(null, new object[] { security }); }
        catch (TargetInvocationException error) { rejected = error.InnerException is UnauthorizedAccessException; }
        Check(rejected, message);
    }

    private static void ExpectDirectoryRejectedBeforeInput(MethodInfo configure, string directory)
    {
        bool read = false, rejected = false;
        Func<byte[]> input = delegate { read = true; throw new InvalidOperationException("Configuration input must remain unread."); };
        try { configure.Invoke(null, new object[] { directory, input }); }
        catch (TargetInvocationException error) { rejected = error.InnerException is UnauthorizedAccessException; }
        Check(rejected && !read, "Missing or unsafe directory is rejected before reading any secret input");
    }

    private static void TestExistingConfigurationFileSecurity()
    {
        MethodInfo validate = typeof(ConfigStore).GetMethod("ValidateConfigurationFileSecurity", BindingFlags.Static | BindingFlags.NonPublic);
        Check(validate != null, "Existing secrets file has a pure ACL validator");
        foreach (bool protect in new bool[] { false, true })
        {
            FileSecurity safe = ConfigurationFileAcl(protect, ServiceSid);
            validate.Invoke(null, new object[] { safe });
            Check(true, "Strict protected or inherited secrets file ACL is accepted");
        }
        ExpectUnsafeAcl(validate, null, "Missing secrets file ACL is rejected");
        FileSecurity empty = new FileSecurity();
        empty.SetOwner(new SecurityIdentifier("S-1-5-32-544"));
        ExpectUnsafeAcl(validate, empty, "Empty secrets file ACL is rejected");
        FileSecurity broad = ConfigurationFileAcl(false, ServiceSid);
        broad.AddAccessRule(new FileSystemAccessRule(new SecurityIdentifier("S-1-1-0"), FileSystemRights.Read, AccessControlType.Allow));
        ExpectUnsafeAcl(validate, broad, "Explicit Everyone read on existing secrets file is rejected");
        FileSecurity wrongOwner = ConfigurationFileAcl(true, ServiceSid);
        wrongOwner.SetOwner(new SecurityIdentifier("S-1-5-21-1-2-3-1001"));
        ExpectUnsafeAcl(validate, wrongOwner, "Untrusted secrets file owner is rejected");
        FileSecurity writableService = ConfigurationFileAcl(true, ServiceSid);
        writableService.AddAccessRule(new FileSystemAccessRule(new SecurityIdentifier(ServiceSid), FileSystemRights.Write, AccessControlType.Allow));
        ExpectUnsafeAcl(validate, writableService, "Service write access on secrets file is rejected");
        ExpectUnsafeAcl(validate, ConfigurationFileAcl(false, "S-1-5-80-1-2-3-4-5"), "Wrong service SID on secrets file is rejected");

        MethodInfo attributes = typeof(ConfigStore).GetMethod("ValidateConfigurationPathAttributes", BindingFlags.Static | BindingFlags.NonPublic);
        Check(attributes != null, "Configuration root and file share a reparse point guard");
        foreach (bool directory in new bool[] { true, false })
        {
            FileAttributes flags = FileAttributes.ReparsePoint | (directory ? FileAttributes.Directory : FileAttributes.Archive);
            bool rejected = false;
            try { attributes.Invoke(null, new object[] { flags, directory }); }
            catch (TargetInvocationException error) { rejected = error.InnerException is UnauthorizedAccessException; }
            Check(rejected, "Root and secrets file reparse points are rejected");
        }

        MethodInfo read = typeof(ConfigStore).GetMethod("ReadConfigurationInput", BindingFlags.Static | BindingFlags.NonPublic);
        Check(read != null, "Configuration preflight can test existing leaf ACL without changing filesystem permissions");
        string directoryPath = Path.Combine(Path.GetTempPath(), "EventDeskPhotoApi-leaf-test-" + Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(directoryPath);
        string filePath = Path.Combine(directoryPath, "secrets.bin");
        Func<string, DirectorySecurity> directoryAcl = delegate(string ignored) { return ConfigurationDirectoryAcl(ServiceSid); };
        try
        {
            bool inputRead = false;
            Func<byte[]> input = delegate { inputRead = true; return new byte[] { 1, 2, 3 }; };
            byte[] result = (byte[])read.Invoke(null, new object[] { directoryPath, input, directoryAcl });
            Check(inputRead && result.Length == 3, "Missing secrets file permits first configuration after root validation");
            File.WriteAllText(filePath, "synthetic-existing-ciphertext");
            inputRead = false;
            bool rejected = false;
            try { read.Invoke(null, new object[] { directoryPath, input, directoryAcl }); }
            catch (TargetInvocationException error) { rejected = error.InnerException is UnauthorizedAccessException; }
            Check(rejected && !inputRead, "Unsafe existing secrets file is rejected before input is read");
            Check(File.ReadAllText(filePath) == "synthetic-existing-ciphertext" && Directory.GetFiles(directoryPath).Length == 1,
                "Rejected existing file is preserved without ACL rewrite or temporary output");
        }
        finally { if (File.Exists(filePath)) File.Delete(filePath); Directory.Delete(directoryPath); }
    }

    private static FileSecurity ConfigurationFileAcl(bool protect, string serviceSid)
    {
        FileSecurity security = new FileSecurity();
        string flags = protect ? "" : "ID";
        security.SetSecurityDescriptorSddlForm("O:BAG:BAD:" + (protect ? "P" : "")
            + "(A;" + flags + ";FA;;;SY)(A;" + flags + ";FA;;;BA)(A;" + flags + ";0x1200a9;;;" + serviceSid + ")");
        return security;
    }

    private static void TestHttpContract()
    {
        FakeRepository repository = new FakeRepository();
        RecordingLog log = new RecordingLog();
        int port = FreePort();
        using (PhotoHttpServer server = new PhotoHttpServer(port, Token, repository, log))
        {
            server.Start();
            Reply reply = Request(port, "/healthz", "GET", null);
            Check(reply.Status == 401 && repository.Reads == 0 && repository.ReadyChecks == 0, "Missing credentials never query SQL (status=" + reply.Status + ", body=" + reply.Body + ")");
            Check(reply.Authenticate == "Bearer", "Unauthorized response includes challenge");
            Check(Request(port, "/readyz", "GET", "Bearer incorrect").Status == 401 && repository.ReadyChecks == 0, "Bad token cannot probe readiness");
            Check(Request(port, "/anything", "POST", "Bearer incorrect").Status == 401, "Every route and method authenticates first");
            Check(Request(port, "/healthz", "GET", "Basic " + Token).Status == 401, "Wrong authentication scheme rejected");
            Check(Request(port, "/healthz", "GET", "Bearer " + Token + "x").Status == 401, "Token suffix rejected");
            reply = Request(port, "/healthz", "GET", "Bearer " + Token);
            Check(reply.Status == 200 && reply.Body == "ok\n" && repository.ReadyChecks == 0, "Health is authenticated liveness only");
            CheckHeaders(reply);
            reply = Request(port, "/healthz", "HEAD", "Bearer " + Token);
            Check(reply.Status == 200 && reply.Bytes.Length == 0 && reply.Length == 3, "HEAD health matches GET headers without body");
            reply = Request(port, "/healthz", "HEAD", null);
            Check(reply.Status == 401 && reply.Bytes.Length == 0 && reply.Length > 0, "Unauthorized HEAD returns no body");
            reply = Request(port, "/readyz", "GET", "Bearer " + Token);
            Check(reply.Status == 200 && repository.ReadyChecks == 1, "Readiness checks repository");
            repository.Ready = false;
            Check(Request(port, "/readyz", "GET", "Bearer " + Token).Status == 503, "Unavailable readiness returns 503");
            repository.Ready = true;
            repository.ThrowReady = true;
            reply = Request(port, "/readyz", "GET", "Bearer " + Token);
            Check(reply.Status == 503 && !reply.Body.Contains("synthetic-secret"), "Readiness error stays private");
            repository.ThrowReady = false;
            reply = Request(port, "/v1/employees/123/photo", "GET", "Bearer " + Token);
            Check(reply.Status == 200 && repository.LastId == "0000123", "Canonical ID passed to repository");
            Check(reply.ContentType == "image/jpeg" && reply.Bytes.Length == 6 && reply.Length == 6, "JPEG bytes and content length returned");
            CheckHeaders(reply);
            reply = Request(port, "/v1/employees/123/photo", "HEAD", "Bearer " + Token);
            Check(reply.Status == 200 && reply.Bytes.Length == 0 && reply.Length == 6, "HEAD photo preserves JPEG length");
            int reads = repository.Reads;
            foreach (string path in new string[] { "/v1/employees/a%2Fb/photo", "/v1/employees/a%20b/photo", "/v1/employees//photo", "/v1/employees/" + new string('x', 51) + "/photo" })
                Check(Request(port, path, "GET", "Bearer " + Token).Status == 400, "Malformed ID returns 400");
            Check(repository.Reads == reads, "Invalid paths do not query repository");
            Check(Request(port, "/unknown", "GET", "Bearer " + Token).Status == 404, "Unknown route returns 404");
            Check(Request(port, "/healthz/", "GET", "Bearer " + Token).Status == 404, "Health path is exact");
            Check(Request(port, "/healthz?query=1", "GET", "Bearer " + Token).Status == 400, "Queries are rejected");
            reply = Request(port, "/healthz", "POST", "Bearer " + Token);
            Check(reply.Status == 405 && reply.Allow == "GET, HEAD", "Unsupported method returns allowed methods");
            repository.Photo = null;
            Check(Request(port, "/v1/employees/1/photo", "GET", "Bearer " + Token).Status == 404, "Missing photo returns 404");
            repository.ThrowRead = true;
            reply = Request(port, "/v1/employees/1/photo", "GET", "Bearer " + Token);
            Check(reply.Status == 503 && !reply.Body.Contains("synthetic-secret"), "Backend failure is sanitized");
            repository.ThrowRead = false;
            repository.Photo = new PhotoData(new byte[PhotoHttpServer.MaxPhotoBytes + 1], "image/jpeg");
            Check(Request(port, "/v1/employees/1/photo", "GET", "Bearer " + Token).Status == 503, "Oversized photo rejected");
            repository.Photo = new PhotoData(new byte[] { 1, 2, 3 }, "image/jpeg");
            Check(Request(port, "/v1/employees/1/photo", "GET", "Bearer " + Token).Status == 503, "Corrupt photo rejected");
            repository.Photo = new PhotoData(new byte[] { 255, 216, 255 }, "image/png");
            Check(Request(port, "/v1/employees/1/photo", "GET", "Bearer " + Token).Status == 503, "Non-JPEG content type rejected");
            repository.Photo = new PhotoData(new byte[0], "image/jpeg");
            reply = Request(port, "/v1/employees/1/photo", "HEAD", "Bearer " + Token);
            Check(reply.Status == 503 && reply.Bytes.Length == 0, "HEAD errors contain no body");
            byte[] maximum = new byte[PhotoHttpServer.MaxPhotoBytes];
            maximum[0] = 255; maximum[1] = 216; maximum[2] = 255;
            repository.Photo = new PhotoData(maximum, "image/jpeg");
            reply = Request(port, "/v1/employees/1/photo", "GET", "Bearer " + Token);
            Check(reply.Status == 200 && reply.Bytes.Length == PhotoHttpServer.MaxPhotoBytes, "Maximum permitted photo size succeeds");
            Check(log.Count >= 4, "Backend/content failures emit sanitized categories");
            Check(!log.SawUnsafeCategory, "Logs contain fixed categories without exception details");
        }
    }

    private static void TestConcurrencyAndShutdown()
    {
        FakeRepository repository = new FakeRepository();
        repository.BlockReads = true;
        int port = FreePort();
        PhotoHttpServer server = new PhotoHttpServer(port, Token, repository, new RecordingLog());
        server.Start();
        List<Thread> clients = new List<Thread>();
        List<int> statuses = new List<int>();
        for (int index = 0; index < 8; index++)
        {
            Thread thread = new Thread(delegate()
            {
                int status = Request(port, "/v1/employees/1/photo", "GET", "Bearer " + Token).Status;
                lock (statuses) statuses.Add(status);
            });
            thread.IsBackground = true;
            clients.Add(thread);
            thread.Start();
        }
        try
        {
            Check(repository.AllEntered.WaitOne(5000), "Eight concurrent requests enter bounded workers");
            Check(Request(port, "/healthz", "GET", "Bearer " + Token).Status == 503, "Ninth concurrent request rejected promptly");
            Check(Request(port, "/healthz", "GET", null).Status == 401, "Authentication remains mandatory while saturated");
            Check(repository.PeakReads == 8, "Repository concurrency limited to eight");
        }
        finally
        {
            repository.ReleaseReads.Set();
            foreach (Thread thread in clients) Check(thread.Join(10000), "Request worker completes");
            server.Dispose();
        }
        Check(statuses.Count == 8 && statuses.TrueForAll(delegate(int value) { return value == 200; }), "Accepted requests finish successfully");
        bool closed = false;
        try { Request(port, "/healthz", "GET", "Bearer " + Token); }
        catch (WebException) { closed = true; }
        Check(closed, "Dispose closes listener");
        server.Dispose();
    }

    private static void ExpectInvalid(SecretSettings settings)
    {
        bool rejected = false;
        try { ConfigStore.Validate(settings); }
        catch (ArgumentException) { rejected = true; }
        Check(rejected, "Unsafe configuration rejected");
    }

    private static void TestServiceShutdownCallback()
    {
        FakeRepository repository = new FakeRepository();
        int port = FreePort();
        PhotoHttpServer server = new PhotoHttpServer(port, Token, repository, new RecordingLog());
        Type serviceType = typeof(PhotoHttpServer).Assembly.GetType("EventDesk.PhotoApi.PhotoWindowsService", true);
        object service = Activator.CreateInstance(serviceType, true);
        serviceType.GetField("server", BindingFlags.Instance | BindingFlags.NonPublic).SetValue(service, server);
        server.Start();
        try
        {
            Check(Request(port, "/healthz", "GET", "Bearer " + Token).Status == 200, "Shutdown fixture listener starts");
            bool completed = true;
            try { serviceType.GetMethod("OnShutdown", BindingFlags.Instance | BindingFlags.NonPublic).Invoke(service, null); }
            catch (TargetInvocationException) { completed = false; }
            Check(completed, "Shutdown callback cleans up without requiring an SCM pending state");
            Check(serviceType.GetField("server", BindingFlags.Instance | BindingFlags.NonPublic).GetValue(service) == null,
                "Shutdown callback releases its server reference");
            bool closed = false;
            try { Request(port, "/healthz", "GET", "Bearer " + Token); }
            catch (WebException) { closed = true; }
            Check(closed, "Shutdown callback closes the active listener");
        }
        finally
        {
            server.Dispose();
            ((IDisposable)service).Dispose();
        }
    }

    private static void TestLoopbackTransportPolicy()
    {
        MethodInfo policy = typeof(PhotoHttpServer).GetMethod("IsLoopbackTransport", BindingFlags.Public | BindingFlags.Static);
        Check(policy != null, "A pure transport endpoint policy exists");
        IPEndPoint ipv4 = new IPEndPoint(IPAddress.Loopback, 1234);
        IPEndPoint ipv6 = new IPEndPoint(IPAddress.IPv6Loopback, 1234);
        IPEndPoint remote = new IPEndPoint(IPAddress.Parse("192.0.2.20"), 1234);
        IPEndPoint local = new IPEndPoint(IPAddress.Parse("192.0.2.21"), 8765);
        Check((bool)policy.Invoke(null, new object[] { ipv4, ipv4 }), "IPv4 loopback endpoints accepted");
        Check((bool)policy.Invoke(null, new object[] { ipv6, ipv6 }), "IPv6 loopback endpoints accepted");
        Check(!(bool)policy.Invoke(null, new object[] { remote, ipv4 }), "Non-loopback remote rejected");
        Check(!(bool)policy.Invoke(null, new object[] { ipv4, local }), "Non-loopback local rejected");
        Check(!(bool)policy.Invoke(null, new object[] { remote, local }), "Two non-loopback endpoints rejected");
        Check(!(bool)policy.Invoke(null, new object[] { null, ipv4 }), "Missing remote endpoint rejected");
        Check(!(bool)policy.Invoke(null, new object[] { ipv4, null }), "Missing local endpoint rejected");
        ConstructorInfo seam = typeof(PhotoHttpServer).GetConstructor(BindingFlags.Instance | BindingFlags.NonPublic, null,
            new Type[] { typeof(int), typeof(string), typeof(IPhotoRepository), typeof(ISafeLog), typeof(Func<HttpListenerRequest, bool>) }, null);
        Check(seam != null, "Transport admission has an internal synthetic endpoint seam");
        foreach (IPEndPoint[] endpoints in new IPEndPoint[][] { new IPEndPoint[] { remote, ipv4 }, new IPEndPoint[] { ipv4, local } })
        {
            FakeRepository repository = new FakeRepository();
            int port = FreePort();
            Func<HttpListenerRequest, bool> transportCheck = delegate(HttpListenerRequest ignored)
            { return (bool)policy.Invoke(null, new object[] { endpoints[0], endpoints[1] }); };
            using (PhotoHttpServer server = (PhotoHttpServer)seam.Invoke(new object[] { port, Token, repository, new RecordingLog(), transportCheck }))
            {
                server.Start();
                Check(Request(port, "/readyz", "GET", "Bearer " + Token).Status == 403, "Non-loopback transport blocks authenticated readiness");
                Check(Request(port, "/v1/employees/123/photo", "GET", "Bearer " + Token).Status == 403, "Non-loopback transport blocks authenticated photos");
                Reply reply = Request(port, "/healthz", "GET", null);
                Check(reply.Status == 403 && reply.Authenticate == null, "Transport rejection occurs before authentication");
                Check(repository.Reads == 0 && repository.ReadyChecks == 0, "Rejected transport cannot query repository");
                CheckHeaders(reply);
            }
        }
    }

    private static void CheckHeaders(Reply reply)
    {
        Check(reply.CacheControl == "private, no-store", "Responses cannot be cached");
        Check(reply.NoSniff == "nosniff", "Content sniffing disabled");
        Check(reply.Cors == null, "CORS is not enabled");
    }

    private static int FreePort()
    {
        TcpListener listener = new TcpListener(IPAddress.Loopback, 0);
        listener.Start();
        int port = ((IPEndPoint)listener.LocalEndpoint).Port;
        listener.Stop();
        return port;
    }

    private static Reply Request(int port, string path, string method, string authorization)
    {
        HttpWebRequest request = (HttpWebRequest)WebRequest.Create("http://127.0.0.1:" + port + path);
        request.Proxy = null;
        request.Method = method;
        if (method == "POST") request.ContentLength = 0;
        request.Timeout = 10000;
        request.KeepAlive = false;
        request.ServicePoint.ConnectionLimit = 32;
        if (authorization != null) request.Headers["Authorization"] = authorization;
        HttpWebResponse response;
        try { response = (HttpWebResponse)request.GetResponse(); }
        catch (WebException error)
        {
            response = error.Response as HttpWebResponse;
            if (response == null) throw;
        }
        using (response)
        using (MemoryStream memory = new MemoryStream())
        {
            response.GetResponseStream().CopyTo(memory);
            return new Reply
            {
                Status = (int)response.StatusCode, Bytes = memory.ToArray(), Length = response.ContentLength,
                ContentType = response.ContentType, CacheControl = response.Headers["Cache-Control"],
                NoSniff = response.Headers["X-Content-Type-Options"], Cors = response.Headers["Access-Control-Allow-Origin"],
                Authenticate = response.Headers["WWW-Authenticate"], Allow = response.Headers["Allow"]
            };
        }
    }

    private static void Check(bool condition, string message)
    {
        if (!condition) throw new InvalidOperationException(message);
        assertions++;
    }

    private sealed class Reply
    {
        public int Status;
        public byte[] Bytes;
        public long Length;
        public string ContentType, CacheControl, NoSniff, Cors, Authenticate, Allow;
        public string Body { get { return Encoding.UTF8.GetString(Bytes); } }
    }

    private sealed class RecordingLog : ISafeLog
    {
        public int Count;
        public bool SawUnsafeCategory;
        public void Write(string category, int number)
        {
            Interlocked.Increment(ref Count);
            if (category != "request_error" && category != "response_error" && category != "listener_error" && category != "sql_error" && category != "invalid_photo")
                SawUnsafeCategory = true;
        }
    }

    private sealed class FakeRepository : IPhotoRepository
    {
        public bool Ready = true, ThrowReady, ThrowRead, BlockReads;
        public int Reads, ReadyChecks, PeakReads;
        public string LastId;
        public PhotoData Photo = new PhotoData(new byte[] { 255, 216, 255, 224, 255, 217 }, "image/jpeg");
        public readonly ManualResetEvent AllEntered = new ManualResetEvent(false);
        public readonly ManualResetEvent ReleaseReads = new ManualResetEvent(false);
        private int active;

        public bool IsReady()
        {
            Interlocked.Increment(ref ReadyChecks);
            if (ThrowReady) throw new InvalidOperationException("synthetic-secret backend details");
            return Ready;
        }

        public PhotoData FindPhoto(string employeeId)
        {
            Interlocked.Increment(ref Reads);
            LastId = employeeId;
            if (ThrowRead) throw new InvalidOperationException("synthetic-secret backend details");
            if (BlockReads)
            {
                int count = Interlocked.Increment(ref active);
                int previous;
                do { previous = PeakReads; if (previous >= count) break; }
                while (Interlocked.CompareExchange(ref PeakReads, count, previous) != previous);
                if (count == 8) AllEntered.Set();
                ReleaseReads.WaitOne(10000);
                Interlocked.Decrement(ref active);
            }
            return Photo;
        }
    }
}
