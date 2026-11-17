using System;
using System.IO;
using System.Net;
using System.Net.Sockets;
using System.Reflection;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using EventDesk.PhotoApi;

internal static class EventApiProxyTests
{
    private static int assertions;
    private static readonly string ApiToken = Guid.NewGuid().ToString("N") + "-fixture-alphabet";
    private static readonly byte[] Envelope = Encoding.UTF8.GetBytes("{\"method\":\"GET\",\"path\":\"/api/me\"}");

    private static int Main()
    {
        try
        {
            TestValidation(); TestBounds(); TestFailuresAndDeadline(); TestFixedTransport(); TestBackendStatuses(); TestHttpContract(); TestConcurrency(); TestShutdownDrain();
            Console.WriteLine("PASS: " + assertions + " event proxy checks; no SQL or external requests.");
            return 0;
        }
        catch (Exception error) { Console.Error.WriteLine("FAIL: " + error.Message); return 1; }
    }

    private static EventProxyReply Call(EventApiProxy proxy, string path, string method, string media, byte[] bytes, long length)
    {
        return proxy.Process(path, method, media, new MemoryStream(bytes), length);
    }

    private static void TestValidation()
    {
        FakeTransport transport = new FakeTransport(); EventApiProxy proxy = new EventApiProxy(transport);
        foreach (string path in new[] { "/v1/event/request", "/v1/event/snapshot" })
        {
            EventProxyReply reply = Call(proxy, path, "POST", "application/json; charset=utf-8", Envelope, Envelope.Length);
            Check(reply.Status == 200 && Encoding.UTF8.GetString(reply.Bytes) == "{\"ok\":true}", "Valid envelopes pass through");
            Check(transport.LastPath == path && transport.LastBody.Length == Envelope.Length, "Fixed route and exact bytes forwarded");
            Check(transport.Timeout > 0 && transport.Timeout <= 30000, "Remaining deadline reaches transport");
        }
        int calls = transport.Calls;
        foreach (string path in new[] { "http://evil.invalid/", "/v1/event/request?url=other", "/v1/event/request/", "/v1/event/other" })
            Check(Call(proxy, path, "POST", "application/json", Envelope, Envelope.Length).Status == 404, "Unknown destinations rejected");
        foreach (string method in new[] { "GET", "HEAD", "PUT", "DELETE" })
            Check(Call(proxy, "/v1/event/request", method, "application/json", Envelope, Envelope.Length).Status == 405, "Event endpoint accepts POST only");
        foreach (string media in new[] { null, "text/plain", "application/jsonp", "application/json; charset=utf-16", "application/json; x=1" })
            Check(Call(proxy, "/v1/event/request", "POST", media, Envelope, Envelope.Length).Status == 415, "Non-JSON media rejected");
        Check(transport.Calls == calls, "Rejected requests do not reach backend");
        Check(Call(proxy, "/v1/event/request", "POST", "APPLICATION/JSON; CHARSET=\"utf-8\"", Envelope, -1).Status == 200, "Chunked JSON and quoted UTF8 charset supported");
    }

    private static void TestBounds()
    {
        FakeTransport transport = new FakeTransport(); EventApiProxy proxy = new EventApiProxy(transport);
        Check(Call(proxy, "/v1/event/request", "POST", "application/json", Envelope, EventApiProxy.MaxRequestBytes + 1L).Status == 413, "Declared oversize rejected");
        Check(transport.Calls == 0, "Declared oversize never reaches backend");
        Check(Call(proxy, "/v1/event/request", "POST", "application/json", new byte[EventApiProxy.MaxRequestBytes + 1], -1).Status == 413, "Chunked overflow rejected");
        Check(Call(proxy, "/v1/event/request", "POST", "application/json", Envelope, Envelope.Length + 1).Status == 400, "Incomplete body rejected");
        Check(Call(proxy, "/v1/event/request", "POST", "application/json", new byte[0], 0).Status == 400, "Empty envelope rejected");
        transport.Response = new byte[EventApiProxy.MaxResponseBytes + 1];
        Check(Call(proxy, "/v1/event/request", "POST", "application/json", Envelope, Envelope.Length).Status == 400, "Oversized backend response fails closed as a protocol error");
        transport.Response = null;
        Check(Call(proxy, "/v1/event/request", "POST", "application/json", Envelope, Envelope.Length).Status == 400, "Missing backend response fails closed as a protocol error");
        Check(EventApiProxy.MaxRequestBytes == 12 * 1024 * 1024 && EventApiProxy.MaxResponseBytes == 24 * 1024 * 1024, "Protocol bounds remain exact");
    }

    private static void TestFailuresAndDeadline()
    {
        FakeTransport transport = new FakeTransport(); RecordingLog log = new RecordingLog(); EventApiProxy proxy = new EventApiProxy(transport, 25, log);
        transport.Throw = true;
        EventProxyReply failed = Call(proxy, "/v1/event/request", "POST", "application/json", Envelope, Envelope.Length);
        Check(failed.Status == 503 && !Encoding.UTF8.GetString(failed.Bytes).Contains("sensitive"), "Backend errors are generic JSON");
        Check(log.Last == "event_upstream_error" && log.Number == 0, "Backend failure logs only a fixed safe category");
        transport.Throw = false; transport.Corrupt = true;
        EventProxyReply corrupt = Call(proxy, "/v1/event/request", "POST", "application/json", Envelope, Envelope.Length);
        Check(corrupt.Status == 400 && !Encoding.UTF8.GetString(corrupt.Bytes).Contains("sensitive"), "Definite backend corruption fails closed instead of enabling outage fallback");
        transport.Corrupt = false; transport.Delay = 50;
        Check(Call(proxy, "/v1/event/request", "POST", "application/json", Envelope, Envelope.Length).Status == 503, "Completed work after deadline is rejected");
        Check(log.Last == "event_timeout", "Deadline failure logs a safe timeout category");
        transport.Delay = 0;
        EventProxyReply slow = proxy.Process("/v1/event/request", "POST", "application/json", new SlowBody(), -1);
        Check(slow.Status == 503, "Slow upload respects total deadline");
        Check(log.Last == "event_timeout", "Slow upload has timeout diagnostics without payload data");
        Check(Call(proxy, "/v1/event/request", "POST", "application/json", Envelope, Envelope.Length).Status == 200, "Slot released after upload/backend failures");
        HttpEventApiTransport real = new HttpEventApiTransport(ApiToken);
        foreach (string path in new[] { "http://evil.invalid/", "/v1/event/request?host=evil", "/healthz" })
        {
            bool rejected = false;
            try { real.Forward(path, Envelope, 100); } catch (ArgumentException) { rejected = true; }
            Check(rejected, "Real transport rejects destination substitution without I/O");
        }
    }

    private static void TestHttpContract()
    {
        FakeTransport transport = new FakeTransport(); int port = FreePort();
        using (PhotoHttpServer server = new PhotoHttpServer(port, ApiToken, new Photos(), new SafeLog(), new EventApiProxy(transport)))
        {
            server.Start();
            Check(Request(port, "/v1/event/request", "POST", null, "application/json", Envelope).Status == 401, "Existing bearer check protects event proxy");
            Check(transport.Calls == 0, "Unauthorized request does not reach event backend");
            foreach (string path in new[] { "/v1/event/request", "/v1/event/snapshot" })
            {
                Reply reply = Request(port, path, "POST", "Bearer " + ApiToken, "application/json", Envelope);
                Check(reply.Status == 200 && reply.Body == "{\"ok\":true}", "Authenticated event route passes response envelope");
                Check(reply.Cache == "private, no-store" && reply.NoSniff == "nosniff" && reply.Cors == null, "Event response retains private security headers");
            }
            Reply badMethod = Request(port, "/v1/event/request", "GET", "Bearer " + ApiToken, null, null);
            Check(badMethod.Status == 405 && badMethod.Allow == "POST", "HTTP method restriction advertises POST");
            Check(Request(port, "/v1/event/request", "POST", "Bearer " + ApiToken, "text/plain", Envelope).Status == 415, "HTTP media validation occurs before backend");
            Check(Request(port, "/healthz", "GET", "Bearer " + ApiToken, null, null).Status == 200, "Photo service health unchanged");
            Check(Request(port, "/v1/employees/123/photo", "GET", "Bearer " + ApiToken, null, null).Status == 200, "Photo route unchanged");
            transport.Throw = true;
            Reply unavailable = Request(port, "/v1/event/request", "POST", "Bearer " + ApiToken, "application/json", Envelope);
            Check(unavailable.Status == 503 && unavailable.Type.StartsWith("application/json") && !unavailable.Body.Contains("sensitive"), "HTTP backend failure remains safe JSON");
            transport.Throw = false;
            foreach (int status in new[] { 400, 401, 403, 404, 405, 409, 413, 415 })
            {
                transport.Status = status;
                Reply denied = Request(port, "/v1/event/request", "POST", "Bearer " + ApiToken, "application/json", Envelope);
                Check(denied.Status == status && denied.Type.StartsWith("application/json"), "Safe backend protocol rejection retains its status: expected=" + status + " actual=" + denied.Status + " type=" + denied.Type);
                Check(denied.Body == "{\"error\":\"Event request rejected.\"}", "Backend protocol failure never exposes upstream content");
            }
        }
    }

    private static void TestFixedTransport()
    {
        HttpEventApiTransport transport = new HttpEventApiTransport(ApiToken);
        MethodInfo factory = typeof(HttpEventApiTransport).GetMethod("CreateRequest", BindingFlags.NonPublic | BindingFlags.Instance);
        HttpWebRequest request = (HttpWebRequest)factory.Invoke(transport, new object[] { "/v1/event/request", Envelope.Length, 15000 });
        Check(request.RequestUri.AbsoluteUri == "http://127.0.0.1:8766/v1/event/request", "Upstream origin is fixed loopback");
        Check(!request.AllowAutoRedirect && request.Proxy == null && !request.UseDefaultCredentials && request.Credentials == null, "Transport cannot redirect credentials or use system proxy/default credentials");
        Check(request.Headers["Authorization"] == "Bearer " + ApiToken && request.Headers["Cookie"] == null, "Only backend bearer is attached, without caller cookies");
        Check(request.Timeout == 15000 && request.ReadWriteTimeout == 15000, "Upstream I/O uses remaining total deadline");
        request.Abort();
        MethodInfo read = typeof(HttpEventApiTransport).GetMethod("ReadResponse", BindingFlags.NonPublic | BindingFlags.Static);
        byte[] result = (byte[])read.Invoke(null, new object[] { new MemoryStream(Envelope), (long)Envelope.Length });
        Check(result.Length == Envelope.Length, "Bounded backend body is preserved");
        foreach (object[] input in new[] {
            new object[] { new MemoryStream(new byte[0]), 0L },
            new object[] { new MemoryStream(Envelope), (long)Envelope.Length + 1 },
            new object[] { new MemoryStream(new byte[EventApiProxy.MaxResponseBytes + 1]), -1L } })
        {
            bool rejected = false;
            try { read.Invoke(null, input); } catch (TargetInvocationException error) { rejected = error.InnerException is InvalidDataException; }
            Check(rejected, "Empty, incomplete, or chunked oversized backend response rejected");
        }
    }

    private static void TestConcurrency()
    {
        FakeTransport transport = new FakeTransport(); transport.Block = true; RecordingLog log = new RecordingLog(); EventApiProxy proxy = new EventApiProxy(transport, log);
        Task<EventProxyReply>[] pending = new Task<EventProxyReply>[4];
        for (int i = 0; i < pending.Length; i++) pending[i] = Task.Factory.StartNew(delegate { return Call(proxy, "/v1/event/request", "POST", "application/json", Envelope, Envelope.Length); });
        Check(transport.AllEntered.WaitOne(5000), "Four event operations can run together");
        try
        {
            Check(Call(proxy, "/v1/event/request", "POST", "application/json", Envelope, Envelope.Length).Status == 503, "Fifth event operation rejected promptly");
            Check(log.Last == "event_busy" && log.Number == 0, "Busy event proxy logs only safe admission diagnostic");
        }
        finally { transport.Release.Set(); }
        Task.WaitAll(pending);
        foreach (Task<EventProxyReply> task in pending) Check(task.Result.Status == 200, "Accepted operation completes");
    }

    private static void TestBackendStatuses()
    {
        int port = FreePort(); int[] statuses = new[] { 400, 401, 403, 404, 405, 409, 413, 415, 500, 302 };
        using (HttpListener backend = new HttpListener())
        {
            backend.Prefixes.Add("http://127.0.0.1:" + port + "/"); backend.IgnoreWriteExceptions = true; backend.Start();
            Task serving = Task.Factory.StartNew(delegate
            {
                foreach (int status in statuses)
                {
                    HttpListenerContext context = backend.GetContext();
                    using (context.Request.InputStream) context.Request.InputStream.CopyTo(Stream.Null);
                    byte[] privateBody = Encoding.UTF8.GetBytes("sensitive backend token SQL details");
                    context.Response.StatusCode = status; context.Response.ContentType = "application/json";
                    context.Response.ContentLength64 = privateBody.Length;
                    context.Response.OutputStream.Write(privateBody, 0, privateBody.Length); context.Response.Close();
                }
            });
            try
            {
                HttpEventApiTransport transport = new HttpEventApiTransport(ApiToken);
                MethodInfo forward = typeof(HttpEventApiTransport).GetMethod("ForwardRequest", BindingFlags.NonPublic | BindingFlags.Instance);
                foreach (int status in statuses)
                {
                    // Exercise real HTTP error-response handling on an ephemeral loopback
                    // listener. The production public Forward entry point stays fixed8766.
                    HttpWebRequest request = (HttpWebRequest)WebRequest.Create("http://127.0.0.1:" + port + "/v1/event/request");
                    request.Method = "POST"; request.Proxy = null; request.AllowAutoRedirect = false;
                    request.ContentType = "application/json"; request.ContentLength = Envelope.Length;
                    bool checkedFailure = false;
                    try { forward.Invoke(transport, new object[] { request, Envelope, 5000 }); }
                    catch (TargetInvocationException failure)
                    {
                        EventApiStatusException safe = failure.InnerException as EventApiStatusException;
                        Check(EventApiStatusException.IsSafeStatus(status) ? safe != null && safe.Status == status : safe == null, "Real backend statuses keep protocol denials distinct from outages");
                        Check(!failure.InnerException.ToString().Contains("sensitive backend"), "Backend error body is never retained in exceptions");
                        checkedFailure = true;
                    }
                    Check(checkedFailure, "Non200 loopback backend cannot be returned as success");
                }
                Check(serving.Wait(5000), "Synthetic backend completed all protocol cases");
            }
            finally { backend.Close(); }
        }
    }

    private static int FreePort() { TcpListener listener = new TcpListener(IPAddress.Loopback, 0); listener.Start(); int port = ((IPEndPoint)listener.LocalEndpoint).Port; listener.Stop(); return port; }

    private static void TestShutdownDrain()
    {
        FakeTransport transport = new FakeTransport(); transport.Block = true; int port = FreePort();
        PhotoHttpServer server = new PhotoHttpServer(port, ApiToken, new Photos(), new SafeLog(), new EventApiProxy(transport));
        server.Start();
        Task<Reply> request = Task.Factory.StartNew(delegate { return Request(port, "/v1/event/request", "POST", "Bearer " + ApiToken, "application/json", Envelope); });
        Check(transport.FirstEntered.WaitOne(5000), "Event request enters before shutdown");
        Task shutdown = Task.Factory.StartNew(delegate { server.Dispose(); });
        try { Check(!shutdown.Wait(50), "Shutdown waits for in-flight event work"); }
        finally { transport.Release.Set(); }
        Check(shutdown.Wait(5000), "Shutdown completes once event work drains");
        // Listener.Close may abort the HTTP connection, but the work must still drain.
        try { request.Wait(5000); } catch (AggregateException) { }
        Check(request.IsCompleted, "Caller is released after shutdown");
        server.Dispose();
    }
    private static void Check(bool value, string message) { if (!value) throw new InvalidOperationException(message); assertions++; }
    private static Reply Request(int port, string path, string method, string authorization, string media, byte[] body)
    {
        HttpWebRequest request = (HttpWebRequest)WebRequest.Create("http://127.0.0.1:" + port + path);
        request.Proxy = null; request.Method = method; request.Timeout = 10000; request.KeepAlive = false;
        if (authorization != null) request.Headers["Authorization"] = authorization;
        if (media != null) request.ContentType = media;
        if (body != null) { request.ContentLength = body.Length; using (Stream stream = request.GetRequestStream()) stream.Write(body, 0, body.Length); }
        HttpWebResponse response;
        try { response = (HttpWebResponse)request.GetResponse(); } catch (WebException error) { response = error.Response as HttpWebResponse; if (response == null) throw; }
        using (response) using (StreamReader reader = new StreamReader(response.GetResponseStream()))
            return new Reply { Status = (int)response.StatusCode, Body = reader.ReadToEnd(), Cache = response.Headers["Cache-Control"], NoSniff = response.Headers["X-Content-Type-Options"], Cors = response.Headers["Access-Control-Allow-Origin"], Type = response.ContentType, Allow = response.Headers["Allow"] };
    }
    private sealed class Reply { public int Status; public string Body, Cache, NoSniff, Cors, Type, Allow; }
    private sealed class Photos : IPhotoRepository { public bool IsReady() { return true; } public PhotoData FindPhoto(string id) { return new PhotoData(new byte[] {255,216,255,217}, "image/jpeg"); } }
    private sealed class SafeLog : ISafeLog { public void Write(string category, int number) { } }
    private sealed class RecordingLog : ISafeLog { public string Last; public int Number; public void Write(string category, int number) { Last = category; Number = number; } }
    private sealed class FakeTransport : IEventApiTransport
    {
        public int Calls, Timeout, Delay, Status; public string LastPath; public byte[] LastBody;
        public byte[] Response = Encoding.UTF8.GetBytes("{\"ok\":true}"); public bool Throw, Block, Corrupt;
        public readonly ManualResetEvent AllEntered = new ManualResetEvent(false), FirstEntered = new ManualResetEvent(false), Release = new ManualResetEvent(false);
        public byte[] Forward(string path, byte[] json, int timeoutMilliseconds)
        {
            int count = Interlocked.Increment(ref Calls); LastPath = path; LastBody = json; Timeout = timeoutMilliseconds;
            FirstEntered.Set();
            if (Throw) throw new InvalidOperationException("sensitive token and SQL detail");
            if (Corrupt) throw new InvalidDataException("sensitive backend protocol details");
            if (Status != 0) throw new EventApiStatusException(Status);
            if (Delay > 0) Thread.Sleep(Delay);
            if (Block) { if (count == 4) AllEntered.Set(); Release.WaitOne(5000); }
            return Response;
        }
    }
    private sealed class SlowBody : MemoryStream
    {
        public override Task<int> ReadAsync(byte[] buffer, int offset, int count, CancellationToken cancellationToken)
        { return Task.Factory.StartNew(delegate { Thread.Sleep(80); return 0; }); }
    }
}
