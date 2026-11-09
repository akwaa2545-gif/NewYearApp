using System;
using System.Data;
using System.Data.SqlClient;
using System.Diagnostics;
using System.Globalization;
using System.IO;
using System.Net;
using System.Runtime.Serialization;
using System.Runtime.Serialization.Json;
using System.Security.AccessControl;
using System.Security.Cryptography;
using System.Security.Principal;
using System.ServiceProcess;
using System.Text;
using System.Threading;

namespace EventDesk.PhotoApi
{
    [DataContract]
    public sealed class SecretSettings
    {
        [DataMember(IsRequired = true)] public string ConnectionString { get; set; }
        [DataMember(IsRequired = true)] public string ApiToken { get; set; }
        [DataMember(IsRequired = true)] public int Port { get; set; }
    }

    public static class ConfigStore
    {
        private const int MaxConfigBytes = 65536;
        private static readonly byte[] Entropy = Encoding.UTF8.GetBytes("EventDeskPhotoApi.Configuration.v1");
        public static readonly string DirectoryPath = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.CommonApplicationData), "EventDeskPhotoApi");
        public static readonly string FilePath = Path.Combine(DirectoryPath, "secrets.bin");

        public static SecretSettings Validate(SecretSettings settings)
        {
            if (settings == null || settings.Port != 8765 || !ValidToken(settings.ApiToken))
                throw new ArgumentException("Invalid API configuration.");
            SqlConnectionStringBuilder builder;
            try { builder = new SqlConnectionStringBuilder(settings.ConnectionString); }
            catch (ArgumentException) { throw new ArgumentException("Invalid SQL configuration."); }
            if (!builder.Encrypt || !String.Equals(builder.InitialCatalog, "Employee_db", StringComparison.OrdinalIgnoreCase)
                || String.IsNullOrWhiteSpace(builder.DataSource) || builder.AttachDBFilename.Length != 0
                || builder.UserInstance || (!builder.IntegratedSecurity && (String.IsNullOrEmpty(builder.UserID) || String.IsNullOrEmpty(builder.Password))))
                throw new ArgumentException("Invalid SQL configuration.");
            builder.ConnectTimeout = 5;
            builder.MaxPoolSize = 8;
            builder.MinPoolSize = 0;
            builder.Pooling = true;
            builder.PersistSecurityInfo = false;
            builder.MultipleActiveResultSets = false;
            builder.ApplicationName = "EventDeskPhotoApi";
            return new SecretSettings { ConnectionString = builder.ConnectionString, ApiToken = settings.ApiToken, Port = 8765 };
        }

        internal static bool ValidToken(string token)
        {
            if (token == null || token.Length < 32 || token.Length > 512) return false;
            bool[] seen = new bool[127];
            int distinct = 0;
            foreach (char character in token)
            {
                if (character < 33 || character > 126) return false;
                if (!seen[character]) { seen[character] = true; distinct++; }
            }
            return distinct >= 12;
        }

        public static SecretSettings Load()
        {
            byte[] protectedBytes = ReadBounded(FilePath);
            byte[] plain = ProtectedData.Unprotect(protectedBytes, Entropy, DataProtectionScope.LocalMachine);
            try { return Validate(Deserialize(plain)); }
            finally { Array.Clear(plain, 0, plain.Length); }
        }

        public static void ConfigureFromStandardInput()
        {
            ConfigureFromStandardInput(DirectoryPath, ReadStandardInput);
        }

        internal static void ConfigureFromStandardInput(string directoryPath, Func<byte[]> readInput)
        {
            byte[] plain = ReadConfigurationInput(directoryPath, readInput,
                delegate(string path) { return Directory.GetAccessControl(path, AccessControlSections.Access | AccessControlSections.Owner); });
            byte[] serialized = null;
            string temporary = null;
            string filePath = Path.Combine(directoryPath, "secrets.bin");
            try
            {
                SecretSettings settings = Validate(Deserialize(plain));
                using (MemoryStream memory = new MemoryStream())
                {
                    new DataContractJsonSerializer(typeof(SecretSettings)).WriteObject(memory, settings);
                    serialized = memory.ToArray();
                }
                byte[] encrypted = ProtectedData.Protect(serialized, Entropy, DataProtectionScope.LocalMachine);
                temporary = Path.Combine(directoryPath, "secrets." + Guid.NewGuid().ToString("N") + ".tmp");
                using (FileStream file = new FileStream(temporary, FileMode.CreateNew, FileAccess.Write, FileShare.None))
                {
                    file.Write(encrypted, 0, encrypted.Length);
                    file.Flush(true);
                }
                if (File.Exists(filePath)) File.Replace(temporary, filePath, null);
                else File.Move(temporary, filePath);
                temporary = null;
            }
            finally
            {
                Array.Clear(plain, 0, plain.Length);
                if (serialized != null) Array.Clear(serialized, 0, serialized.Length);
                if (temporary != null && File.Exists(temporary)) File.Delete(temporary);
            }
        }

        internal static void ValidateConfigurationDirectorySecurity(DirectorySecurity security)
        {
            if (security == null || !security.AreAccessRulesProtected)
                throw new UnauthorizedAccessException("Configuration directory permissions do not match the protected installation.");
            ValidateConfigurationSecurity(security, true);
        }

        internal static void ValidateConfigurationFileSecurity(FileSecurity security)
        {
            ValidateConfigurationSecurity(security, false);
        }

        private static void ValidateConfigurationSecurity(FileSystemSecurity security, bool directory)
        {
            if (security == null || !new SecurityIdentifier("S-1-5-32-544").Equals(security.GetOwner(typeof(SecurityIdentifier))))
                throw new UnauthorizedAccessException("Configuration permissions do not match the protected installation.");
            string serviceSid = GetPhotoServiceSid();
            AuthorizationRuleCollection rules = security.GetAccessRules(true, true, typeof(SecurityIdentifier));
            bool system = false, administrators = false, service = false;
            foreach (FileSystemAccessRule rule in rules)
            {
                string sid = rule.IdentityReference.Value;
                bool isService = sid == serviceSid;
                FileSystemRights expected = isService ? FileSystemRights.ReadAndExecute | FileSystemRights.Synchronize : FileSystemRights.FullControl;
                if (rule.AccessControlType != AccessControlType.Allow || (directory && rule.IsInherited)
                    || (sid != "S-1-5-18" && sid != "S-1-5-32-544" && !isService)
                    || rule.FileSystemRights != expected
                    || rule.InheritanceFlags != (directory ? InheritanceFlags.ContainerInherit | InheritanceFlags.ObjectInherit : InheritanceFlags.None)
                    || rule.PropagationFlags != PropagationFlags.None)
                    throw new UnauthorizedAccessException("Configuration directory permissions do not match the protected installation.");
                if (sid == "S-1-5-18") system = true;
                if (sid == "S-1-5-32-544") administrators = true;
                if (isService) service = true;
            }
            if (rules.Count != 3 || !system || !administrators || !service)
                throw new UnauthorizedAccessException("Configuration directory permissions do not match the protected installation.");
        }

        internal static byte[] ReadConfigurationInput(string directoryPath, Func<byte[]> readInput, Func<string, DirectorySecurity> readDirectorySecurity)
        {
            if (!Directory.Exists(directoryPath))
                throw new UnauthorizedAccessException("Install the protected configuration directory before configuring secrets.");
            ValidateConfigurationPathAttributes(File.GetAttributes(directoryPath), true);
            ValidateConfigurationDirectorySecurity(readDirectorySecurity(directoryPath));
            string filePath = Path.Combine(directoryPath, "secrets.bin");
            FileAttributes attributes;
            try { attributes = File.GetAttributes(filePath); }
            catch (FileNotFoundException) { return readInput(); }
            ValidateConfigurationPathAttributes(attributes, false);
            ValidateConfigurationFileSecurity(File.GetAccessControl(filePath, AccessControlSections.Access | AccessControlSections.Owner));
            return readInput();
        }

        internal static void ValidateConfigurationPathAttributes(FileAttributes attributes, bool directory)
        {
            if ((attributes & FileAttributes.ReparsePoint) != 0 || ((attributes & FileAttributes.Directory) != 0) != directory)
                throw new UnauthorizedAccessException("Configuration paths must be regular protected directories and files.");
        }

        private static string GetPhotoServiceSid()
        {
            byte[] hash;
            using (SHA1 sha = SHA1.Create())
                hash = sha.ComputeHash(Encoding.Unicode.GetBytes("EventDeskPhotoApi".ToUpperInvariant()));
            StringBuilder sid = new StringBuilder("S-1-5-80");
            for (int offset = 0; offset < hash.Length; offset += 4)
                sid.Append('-').Append(BitConverter.ToUInt32(hash, offset).ToString(CultureInfo.InvariantCulture));
            return sid.ToString();
        }

        private static SecretSettings Deserialize(byte[] bytes)
        {
            using (MemoryStream memory = new MemoryStream(bytes, false))
                return (SecretSettings)new DataContractJsonSerializer(typeof(SecretSettings)).ReadObject(memory);
        }

        private static byte[] ReadBounded(string path)
        {
            using (FileStream file = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.Read))
            {
                if (file.Length <= 0 || file.Length > MaxConfigBytes) throw new InvalidDataException("Invalid configuration size.");
                byte[] bytes = new byte[(int)file.Length];
                int position = 0;
                while (position < bytes.Length)
                {
                    int read = file.Read(bytes, position, bytes.Length - position);
                    if (read == 0) throw new InvalidDataException("Incomplete configuration.");
                    position += read;
                }
                return bytes;
            }
        }

        private static byte[] ReadStandardInput()
        {
            using (Stream input = Console.OpenStandardInput())
            using (MemoryStream memory = new MemoryStream())
            {
                byte[] buffer = new byte[4096];
                int read;
                while ((read = input.Read(buffer, 0, buffer.Length)) != 0)
                {
                    if (memory.Length + read > MaxConfigBytes) throw new InvalidDataException("Configuration is too large.");
                    memory.Write(buffer, 0, read);
                }
                return memory.ToArray();
            }
        }
    }

    public static class EmployeeId
    {
        public static bool TryNormalize(string value, out string normalized)
        {
            normalized = null;
            if (String.IsNullOrEmpty(value) || value.Length > 50) return false;
            bool numeric = true;
            foreach (char character in value)
            {
                bool digit = character >= '0' && character <= '9';
                if (!digit && !(character >= 'A' && character <= 'Z') && !(character >= 'a' && character <= 'z') && character != '_' && character != '-') return false;
                if (!digit) numeric = false;
            }
            normalized = numeric && value.Length < 7 ? value.PadLeft(7, '0') : value;
            return true;
        }
    }

    public sealed class PhotoData
    {
        public readonly byte[] Bytes;
        public readonly string ContentType;
        public PhotoData(byte[] bytes, string contentType) { Bytes = bytes; ContentType = contentType; }
    }

    public interface IPhotoRepository
    {
        bool IsReady();
        PhotoData FindPhoto(string employeeId);
    }

    public interface ISafeLog { void Write(string category, int number); }

    public sealed class SqlPhotoRepository : IPhotoRepository
    {
        private readonly string connectionString;
        public SqlPhotoRepository(string value) { connectionString = value; }

        public bool IsReady()
        {
            using (SqlConnection connection = new SqlConnection(connectionString))
            using (SqlCommand command = connection.CreateCommand())
            {
                command.CommandTimeout = 5;
                command.CommandText = "SELECT CASE WHEN OBJECT_ID(N'dbo.EventDeskEmployeePhoto', N'U') IS NOT NULL AND HAS_PERMS_BY_NAME(N'dbo.EventDeskEmployeePhoto', N'OBJECT', N'SELECT') = 1 THEN 1 ELSE 0 END";
                connection.Open();
                if (Convert.ToInt32(command.ExecuteScalar(), CultureInfo.InvariantCulture) != 1) return false;
                command.CommandText = "SELECT TOP (0) EmployeeId, Photo, PhotoHash, ContentType, UpdatedAt, ImportBatchId FROM dbo.EventDeskEmployeePhoto";
                using (SqlDataReader reader = command.ExecuteReader()) { return reader.FieldCount == 6; }
            }
        }

        public PhotoData FindPhoto(string employeeId)
        {
            using (SqlConnection connection = new SqlConnection(connectionString))
            using (SqlCommand command = connection.CreateCommand())
            {
                command.CommandTimeout = 5;
                command.CommandText = "SELECT DATALENGTH(Photo), ContentType, Photo FROM dbo.EventDeskEmployeePhoto WHERE EmployeeId = @employeeId";
                command.Parameters.Add("@employeeId", SqlDbType.VarChar, 50).Value = employeeId;
                connection.Open();
                using (SqlDataReader reader = command.ExecuteReader(CommandBehavior.SingleRow | CommandBehavior.SequentialAccess))
                {
                    if (!reader.Read()) return null;
                    if (reader.IsDBNull(0)) throw new InvalidDataException("Invalid stored photo.");
                    long length = reader.GetInt64(0);
                    if (length < 3 || length > PhotoHttpServer.MaxPhotoBytes) throw new InvalidDataException("Invalid stored photo size.");
                    if (reader.IsDBNull(1)) throw new InvalidDataException("Invalid stored photo type.");
                    string contentType = reader.GetString(1);
                    if (!String.Equals(contentType, "image/jpeg", StringComparison.Ordinal)) throw new InvalidDataException("Invalid stored photo type.");
                    byte[] bytes = new byte[(int)length];
                    long offset = 0;
                    while (offset < length)
                    {
                        long read = reader.GetBytes(2, offset, bytes, (int)offset, (int)(length - offset));
                        if (read == 0) throw new InvalidDataException("Incomplete stored photo.");
                        offset += read;
                    }
                    return new PhotoData(bytes, contentType);
                }
            }
        }
    }

    public sealed class RollingSafeLog : ISafeLog
    {
        private readonly object sync = new object();
        private readonly string path;
        private const long MaxLogBytes = 5 * 1024 * 1024;
        public RollingSafeLog(string directory) { path = Path.Combine(directory, "app.log"); }

        public void Write(string category, int number)
        {
            string safeCategory = "internal_error";
            if (!String.IsNullOrEmpty(category) && category.Length <= 40)
            {
                bool valid = true;
                foreach (char character in category)
                    if (!(character >= 'a' && character <= 'z') && character != '_') valid = false;
                if (valid) safeCategory = category;
            }
            string line = DateTime.UtcNow.ToString("o", CultureInfo.InvariantCulture) + " " + safeCategory + " " + number.ToString(CultureInfo.InvariantCulture) + Environment.NewLine;
            lock (sync)
            {
                try
                {
                    if (File.Exists(path) && new FileInfo(path).Length + Encoding.UTF8.GetByteCount(line) > MaxLogBytes)
                    {
                        if (File.Exists(path + ".1")) File.Delete(path + ".1");
                        File.Move(path, path + ".1");
                    }
                    File.AppendAllText(path, line, new UTF8Encoding(false));
                }
                catch (IOException) { Trace.WriteLine("EventDeskPhotoApi log_unavailable"); }
                catch (UnauthorizedAccessException) { Trace.WriteLine("EventDeskPhotoApi log_unavailable"); }
                catch (System.Security.SecurityException) { Trace.WriteLine("EventDeskPhotoApi log_unavailable"); }
            }
        }
    }

    public sealed class PhotoHttpServer : IDisposable
    {
        public const int MaxPhotoBytes = 256 * 1024;
        private readonly HttpListener listener = new HttpListener();
        private readonly SemaphoreSlim workers = new SemaphoreSlim(8, 8);
        private readonly ManualResetEventSlim drained = new ManualResetEventSlim(true);
        private readonly object lifecycle = new object();
        private readonly string token;
        private readonly IPhotoRepository repository;
        private readonly ISafeLog log;
        private readonly Func<HttpListenerRequest, bool> isLoopbackRequest;
        private Thread acceptThread;
        private int active;
        private volatile bool stopping;

        public PhotoHttpServer(int port, string apiToken, IPhotoRepository photoRepository, ISafeLog safeLog)
            : this(port, apiToken, photoRepository, safeLog,
                delegate(HttpListenerRequest request) { return IsLoopbackTransport(request.RemoteEndPoint, request.LocalEndPoint); })
        { }

        internal PhotoHttpServer(int port, string apiToken, IPhotoRepository photoRepository, ISafeLog safeLog,
            Func<HttpListenerRequest, bool> transportCheck)
        {
            if (port < 1 || port > 65535 || !ConfigStore.ValidToken(apiToken) || photoRepository == null || safeLog == null || transportCheck == null)
                throw new ArgumentException("Invalid server configuration.");
            token = apiToken;
            repository = photoRepository;
            log = safeLog;
            isLoopbackRequest = transportCheck;
            listener.Prefixes.Add("http://127.0.0.1:" + port.ToString(CultureInfo.InvariantCulture) + "/");
            listener.IgnoreWriteExceptions = true;
        }

        public static bool IsLoopbackTransport(IPEndPoint remote, IPEndPoint local)
        {
            return remote != null && local != null && IPAddress.IsLoopback(remote.Address) && IPAddress.IsLoopback(local.Address);
        }

        public void Start()
        {
            lock (lifecycle)
            {
                if (stopping || acceptThread != null) throw new InvalidOperationException("Server cannot restart.");
                listener.Start();
                acceptThread = new Thread(AcceptLoop);
                acceptThread.IsBackground = true;
                acceptThread.Name = "PhotoApiAccept";
                acceptThread.Start();
            }
        }

        private void AcceptLoop()
        {
            while (!stopping)
            {
                HttpListenerContext context;
                try { context = listener.GetContext(); }
                catch (HttpListenerException) { if (!stopping) log.Write("listener_error", 0); return; }
                catch (ObjectDisposedException) { return; }
                if (!isLoopbackRequest(context.Request))
                {
                    try { SendText(context, 403, "forbidden\n"); }
                    catch (Exception error) { LogFailure(error, "response_error"); }
                    finally { Close(context); }
                    continue;
                }
                if (!IsAuthorized(context))
                {
                    try
                    {
                        context.Response.AddHeader("WWW-Authenticate", "Bearer");
                        SendText(context, 401, "unauthorized\n");
                    }
                    catch (Exception error) { LogFailure(error, "response_error"); }
                    finally { Close(context); }
                    continue;
                }
                if (!workers.Wait(0))
                {
                    try { SendText(context, 503, "unavailable\n"); }
                    catch (Exception error) { LogFailure(error, "response_error"); }
                    finally { Close(context); }
                    continue;
                }
                lock (lifecycle) { active++; drained.Reset(); }
                if (!ThreadPool.QueueUserWorkItem(HandleWork, context))
                {
                    Close(context);
                    CompleteWork();
                }
            }
        }

        private void HandleWork(object state)
        {
            HttpListenerContext context = (HttpListenerContext)state;
            try { Handle(context); }
            catch (Exception error)
            {
                LogFailure(error, "request_error");
                try { SendText(context, 503, "unavailable\n"); }
                catch (Exception writeError) { LogFailure(writeError, "response_error"); }
            }
            finally { try { Close(context); } finally { CompleteWork(); } }
        }

        private void CompleteWork()
        {
            workers.Release();
            lock (lifecycle) { active--; if (active == 0) drained.Set(); }
        }

        private bool IsAuthorized(HttpListenerContext context)
        {
            string authorization = context.Request.Headers["Authorization"];
            return authorization != null && authorization.StartsWith("Bearer ", StringComparison.OrdinalIgnoreCase)
                && FixedTimeEquals(authorization.Substring(7), token);
        }

        private void Handle(HttpListenerContext context)
        {
            string method = context.Request.HttpMethod;
            if (method != "GET" && method != "HEAD")
            {
                context.Response.AddHeader("Allow", "GET, HEAD");
                SendText(context, 405, "method not allowed\n");
                return;
            }
            string path = context.Request.RawUrl;
            if (String.IsNullOrEmpty(path) || path.IndexOf('?') >= 0 || context.Request.HasEntityBody)
            {
                SendText(context, 400, "bad request\n");
                return;
            }
            if (path == "/healthz") { SendText(context, 200, "ok\n"); return; }
            if (path == "/readyz")
            {
                bool ready = repository.IsReady();
                SendText(context, ready ? 200 : 503, ready ? "ready\n" : "unavailable\n");
                return;
            }
            const string prefix = "/v1/employees/";
            const string suffix = "/photo";
            if (!path.StartsWith(prefix, StringComparison.Ordinal) || !path.EndsWith(suffix, StringComparison.Ordinal))
            {
                SendText(context, 404, "not found\n");
                return;
            }
            string id;
            int idLength = path.Length - prefix.Length - suffix.Length;
            if (idLength <= 0 || !EmployeeId.TryNormalize(path.Substring(prefix.Length, idLength), out id))
            {
                SendText(context, 400, "bad request\n");
                return;
            }
            PhotoData photo = repository.FindPhoto(id);
            if (photo == null) { SendText(context, 404, "not found\n"); return; }
            if (!ValidPhoto(photo))
            {
                log.Write("invalid_photo", 0);
                SendText(context, 503, "unavailable\n");
                return;
            }
            Send(context, 200, "image/jpeg", photo.Bytes);
        }

        private static bool ValidPhoto(PhotoData photo)
        {
            return photo.Bytes != null && photo.Bytes.Length >= 3 && photo.Bytes.Length <= MaxPhotoBytes
                && String.Equals(photo.ContentType, "image/jpeg", StringComparison.Ordinal)
                && photo.Bytes[0] == 255 && photo.Bytes[1] == 216 && photo.Bytes[2] == 255;
        }

        private static bool FixedTimeEquals(string supplied, string expected)
        {
            int difference = supplied.Length ^ expected.Length;
            for (int index = 0; index < expected.Length; index++)
                difference |= expected[index] ^ (index < supplied.Length ? supplied[index] : 0);
            return difference == 0;
        }

        private static void SendText(HttpListenerContext context, int status, string text)
        {
            Send(context, status, "text/plain; charset=utf-8", Encoding.UTF8.GetBytes(text));
        }

        private static void Send(HttpListenerContext context, int status, string contentType, byte[] bytes)
        {
            HttpListenerResponse response = context.Response;
            response.StatusCode = status;
            response.ContentType = contentType;
            response.ContentLength64 = bytes.Length;
            response.KeepAlive = false;
            response.Headers["Cache-Control"] = "private, no-store";
            response.Headers["X-Content-Type-Options"] = "nosniff";
            if (context.Request.HttpMethod != "HEAD") response.OutputStream.Write(bytes, 0, bytes.Length);
        }

        private static void Close(HttpListenerContext context)
        {
            try { context.Response.Close(); }
            catch (HttpListenerException) { }
            catch (ObjectDisposedException) { }
        }

        private void LogFailure(Exception error, string category)
        {
            SqlException sql = error as SqlException;
            log.Write(sql != null ? "sql_error" : category, sql != null ? sql.Number : 0);
        }

        public void Dispose()
        {
            Thread thread;
            lock (lifecycle)
            {
                if (stopping) return;
                stopping = true;
                listener.Close();
                thread = acceptThread;
            }
            if (thread != null && thread != Thread.CurrentThread) thread.Join(5000);
            drained.Wait(15000);
        }
    }

    internal sealed class PhotoWindowsService : ServiceBase
    {
        private PhotoHttpServer server;
        public PhotoWindowsService() { ServiceName = "EventDeskPhotoApi"; AutoLog = false; CanStop = true; CanShutdown = true; }
        protected override void OnStart(string[] args) { server = Program.CreateServer(); server.Start(); }
        protected override void OnStop()
        {
            try { if (server != null) RequestAdditionalTime(25000); }
            finally { StopServer(); }
        }
        protected override void OnShutdown() { StopServer(); base.OnShutdown(); }
        protected override void Dispose(bool disposing) { if (disposing) StopServer(); base.Dispose(disposing); }
        private void StopServer()
        {
            PhotoHttpServer stoppingServer = Interlocked.Exchange(ref server, null);
            if (stoppingServer != null) stoppingServer.Dispose();
        }
    }

    internal static class Program
    {
        internal static PhotoHttpServer CreateServer()
        {
            SecretSettings settings = ConfigStore.Load();
            return new PhotoHttpServer(settings.Port, settings.ApiToken, new SqlPhotoRepository(settings.ConnectionString),
                new RollingSafeLog(Path.Combine(ConfigStore.DirectoryPath, "logs")));
        }

        private static int Main(string[] args)
        {
            try
            {
                if (args.Length == 0) { ServiceBase.Run(new PhotoWindowsService()); return 0; }
                if (args.Length == 1 && args[0] == "--configure")
                {
                    ConfigStore.ConfigureFromStandardInput();
                    Console.WriteLine("Protected configuration saved.");
                    return 0;
                }
                if (args.Length == 1 && args[0] == "--console")
                {
                    using (ManualResetEvent stop = new ManualResetEvent(false))
                    using (PhotoHttpServer server = CreateServer())
                    {
                        ConsoleCancelEventHandler cancel = delegate(object sender, ConsoleCancelEventArgs eventArgs) { eventArgs.Cancel = true; stop.Set(); };
                        Console.CancelKeyPress += cancel;
                        try { server.Start(); Console.WriteLine("Photo API listening on loopback. Press Ctrl+C to stop."); stop.WaitOne(); }
                        finally { Console.CancelKeyPress -= cancel; }
                    }
                    return 0;
                }
                Console.Error.WriteLine("Usage: EventDeskPhotoApi.exe [--configure | --console]. Configuration JSON is accepted only through standard input.");
                return 2;
            }
            catch (Exception)
            {
                Console.Error.WriteLine("Photo API operation failed. Check protected configuration, directory permissions and loopback URL reservation.");
                return 1;
            }
        }
    }
}
