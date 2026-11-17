// Extends the existing authenticated photo listener with a fixed loopback event backend.
using System;
using System.Diagnostics;
using System.IO;
using System.Net;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading;
using System.Threading.Tasks;

namespace EventDesk.PhotoApi
{
    public interface IEventApiTransport
    {
        byte[] Forward(string path, byte[] json, int timeoutMilliseconds);
    }

    public sealed class EventProxyReply
    {
        public readonly int Status;
        public readonly byte[] Bytes;
        public EventProxyReply(int status, byte[] bytes) { Status = status; Bytes = bytes; }
    }

    // Carries only an allowlisted protocol status; never retains an upstream body.
    public sealed class EventApiStatusException : Exception
    {
        public readonly int Status;
        public EventApiStatusException(int status) : base("Event backend rejected the request.")
        {
            if (!IsSafeStatus(status)) throw new ArgumentException("Invalid event status.");
            Status = status;
        }
        public static bool IsSafeStatus(int status)
        {
            return status == 400 || status == 401 || status == 403 || status == 404
                || status == 405 || status == 409 || status == 413 || status == 415;
        }
    }

    public sealed class EventApiProxy
    {
        public const int MaxRequestBytes = 12 * 1024 * 1024;
        public const int MaxResponseBytes = 24 * 1024 * 1024;
        private readonly IEventApiTransport transport;
        private readonly int deadlineMilliseconds;
        private readonly ISafeLog log;
        private readonly SemaphoreSlim slots = new SemaphoreSlim(4, 4);
        private static readonly Regex JsonMedia = new Regex("\\Aapplication/json(?:\\s*;\\s*charset\\s*=\\s*(?:utf-8|\"utf-8\"))?\\z", RegexOptions.IgnoreCase | RegexOptions.CultureInvariant);

        public EventApiProxy(IEventApiTransport value) : this(value, 30000, null) { }
        public EventApiProxy(IEventApiTransport value, ISafeLog safeLog) : this(value, 30000, safeLog) { }
        public EventApiProxy(IEventApiTransport value, int deadline) : this(value, deadline, null) { }
        public EventApiProxy(IEventApiTransport value, int deadline, ISafeLog safeLog)
        {
            if (value == null || deadline < 1 || deadline > 30000) throw new ArgumentException("Invalid event proxy configuration.");
            transport = value; deadlineMilliseconds = deadline; log = safeLog;
        }

        public static bool IsRoute(string path)
        {
            return path == "/v1/event/request" || path == "/v1/event/snapshot";
        }

        public static bool IsJson(string contentType)
        {
            return contentType != null && contentType.Length <= 128 && JsonMedia.IsMatch(contentType.Trim());
        }

        public EventProxyReply Process(string path, string method, string contentType, Stream body, long contentLength)
        {
            Stopwatch deadline = Stopwatch.StartNew();
            if (!IsRoute(path)) return Failure(404, "Not found.");
            if (method != "POST") return Failure(405, "Method not allowed.");
            if (!IsJson(contentType)) return Failure(415, "Use application/json.");
            if (contentLength > MaxRequestBytes) return Failure(413, "Request is too large.");
            if (contentLength == 0 || contentLength < -1 || body == null) return Failure(400, "Invalid request.");
            if (!slots.Wait(0)) return Unavailable("event_busy");
            try
            {
                byte[] bytes;
                using (body) bytes = ReadRequest(body, contentLength, deadline);
                if (bytes.Length == 0) return Failure(400, "Invalid request.");
                byte[] result = transport.Forward(path, bytes, Remaining(deadline));
                Remaining(deadline);
                if (result == null || result.Length == 0 || result.Length > MaxResponseBytes)
                    return Failure(400, "Invalid event response.");
                return new EventProxyReply(200, result);
            }
            catch (BodyTooLargeException) { return Failure(413, "Request is too large."); }
            catch (IncompleteBodyException) { return Failure(400, "Invalid request."); }
            catch (EventApiStatusException error) { return Failure(error.Status, "Event request rejected."); }
            catch (InvalidDataException)
            {
                return deadline.ElapsedMilliseconds >= deadlineMilliseconds ? Unavailable("event_timeout")
                    : Failure(400, "Invalid event response.");
            }
            catch (TimeoutException) { return Unavailable("event_timeout"); }
            catch (Exception) { return Unavailable(deadline.ElapsedMilliseconds >= deadlineMilliseconds ? "event_timeout" : "event_upstream_error"); }
            finally { slots.Release(); }
        }

        public void Handle(HttpListenerContext context)
        {
            EventProxyReply reply = Process(context.Request.RawUrl, context.Request.HttpMethod,
                context.Request.ContentType, context.Request.InputStream, context.Request.ContentLength64);
            HttpListenerResponse response = context.Response;
            response.StatusCode = reply.Status;
            response.ContentType = "application/json; charset=utf-8";
            response.ContentLength64 = reply.Bytes.Length;
            response.KeepAlive = false;
            response.Headers["Cache-Control"] = "private, no-store";
            response.Headers["X-Content-Type-Options"] = "nosniff";
            if (reply.Status == 405) response.Headers["Allow"] = "POST";
            if (reply.Status == 401) response.AddHeader("WWW-Authenticate", "Bearer");
            if (context.Request.HttpMethod != "HEAD") response.OutputStream.Write(reply.Bytes, 0, reply.Bytes.Length);
        }

        private int Remaining(Stopwatch deadline)
        {
            long remaining = deadlineMilliseconds - deadline.ElapsedMilliseconds;
            if (remaining <= 0) throw new TimeoutException();
            return (int)remaining;
        }

        private byte[] ReadRequest(Stream stream, long expected, Stopwatch deadline)
        {
            using (MemoryStream bytes = new MemoryStream())
            {
                byte[] buffer = new byte[16384];
                while (true)
                {
                    int remaining = Remaining(deadline);
                    Task<int> reading = stream.ReadAsync(buffer, 0, Math.Min(buffer.Length, MaxRequestBytes - (int)bytes.Length + 1));
                    if (!reading.Wait(remaining))
                    {
                        // Closing the request stream cancels pending I/O; observe a late fault.
                        reading.ContinueWith(delegate(Task<int> completed) { GC.KeepAlive(completed.Exception); }, TaskContinuationOptions.OnlyOnFaulted);
                        throw new TimeoutException();
                    }
                    int read = reading.Result;
                    if (read == 0) break;
                    if (bytes.Length + read > MaxRequestBytes) throw new BodyTooLargeException();
                    bytes.Write(buffer, 0, read);
                }
                if (expected >= 0 && bytes.Length != expected) throw new IncompleteBodyException();
                return bytes.ToArray();
            }
        }

        private static EventProxyReply Failure(int status, string message)
        {
            return new EventProxyReply(status, Encoding.UTF8.GetBytes("{\"error\":\"" + message + "\"}"));
        }

        private EventProxyReply Unavailable(string category)
        {
            try { if (log != null) log.Write(category, 0); }
            catch (Exception) { Trace.WriteLine("EventDeskPhotoApi event_log_unavailable"); }
            return Failure(503, "Event service unavailable.");
        }
        private sealed class BodyTooLargeException : Exception { }
        private sealed class IncompleteBodyException : Exception { }
    }

    public sealed class HttpEventApiTransport : IEventApiTransport
    {
        private readonly string apiToken;
        public HttpEventApiTransport(string token)
        {
            if (!ConfigStore.ValidToken(token)) throw new ArgumentException("Invalid event proxy configuration.");
            apiToken = token;
        }

        public byte[] Forward(string path, byte[] json, int timeoutMilliseconds)
        {
            if (!EventApiProxy.IsRoute(path) || json == null || json.Length == 0 || json.Length > EventApiProxy.MaxRequestBytes
                || timeoutMilliseconds < 1 || timeoutMilliseconds > 30000)
                throw new ArgumentException("Invalid event request.");
            HttpWebRequest request = CreateRequest(path, json.Length, timeoutMilliseconds);
            return ForwardRequest(request, json, timeoutMilliseconds);
        }

        private byte[] ForwardRequest(HttpWebRequest request, byte[] json, int timeoutMilliseconds)
        {
            using (Timer abort = new Timer(delegate { request.Abort(); }, null, timeoutMilliseconds, Timeout.Infinite))
            {
                try
                {
                    using (Stream upload = request.GetRequestStream()) upload.Write(json, 0, json.Length);
                    using (HttpWebResponse response = (HttpWebResponse)request.GetResponse())
                    {
                        if (response.StatusCode != HttpStatusCode.OK || !EventApiProxy.IsJson(response.ContentType)
                            || response.ContentLength > EventApiProxy.MaxResponseBytes
                            || !(String.IsNullOrEmpty(response.ContentEncoding) || response.ContentEncoding == "identity"))
                            throw new InvalidDataException("Invalid event response.");
                        return ReadResponse(response.GetResponseStream(), response.ContentLength);
                    }
                }
                catch (WebException error)
                {
                    HttpWebResponse failed = error.Response as HttpWebResponse;
                    int status = failed == null ? 0 : (int)failed.StatusCode;
                    if (error.Response != null) error.Response.Close();
                    if (EventApiStatusException.IsSafeStatus(status)) throw new EventApiStatusException(status);
                    if (status >= 300 && status < 500) throw new EventApiStatusException(400);
                    throw;
                }
                finally { request.Abort(); }
            }
        }

        private HttpWebRequest CreateRequest(string path, int length, int timeoutMilliseconds)
        {
            HttpWebRequest request = (HttpWebRequest)WebRequest.Create("http://127.0.0.1:8766" + path);
            request.Method = "POST";
            request.Proxy = null;
            request.AllowAutoRedirect = false;
            request.UseDefaultCredentials = false;
            request.Credentials = null;
            request.KeepAlive = false;
            request.AllowWriteStreamBuffering = false;
            request.Timeout = timeoutMilliseconds;
            request.ReadWriteTimeout = timeoutMilliseconds;
            request.ContentType = "application/json; charset=utf-8";
            request.ContentLength = length;
            request.Headers["Authorization"] = "Bearer " + apiToken;
            request.ServicePoint.ConnectionLimit = 8;
            return request;
        }

        private static byte[] ReadResponse(Stream stream, long expected)
        {
            using (stream)
            using (MemoryStream bytes = new MemoryStream())
            {
                byte[] buffer = new byte[16384];
                while (true)
                {
                    int read = stream.Read(buffer, 0, Math.Min(buffer.Length, EventApiProxy.MaxResponseBytes - (int)bytes.Length + 1));
                    if (read == 0) break;
                    if (bytes.Length + read > EventApiProxy.MaxResponseBytes) throw new InvalidDataException("Invalid event response.");
                    bytes.Write(buffer, 0, read);
                }
                if (bytes.Length == 0 || (expected >= 0 && bytes.Length != expected)) throw new InvalidDataException("Invalid event response.");
                return bytes.ToArray();
            }
        }
    }
}
