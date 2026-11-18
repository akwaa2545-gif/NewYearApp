// Supervises the private SQL application backend behind the existing photo API.
using System;
using System.Collections.Generic;
using System.Data.Odbc;
using System.Data.SqlClient;
using System.Diagnostics;
using System.IO;
using System.Net;
using System.Runtime.InteropServices;
using System.Runtime.Serialization;
using System.Runtime.Serialization.Json;
using System.Security.AccessControl;
using System.Security.Principal;
using System.ServiceProcess;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading;
using System.Threading.Tasks;
using System.Xml;
using Microsoft.Win32;
using Microsoft.Win32.SafeHandles;
using EventDesk.PhotoApi;

namespace EventDesk.EventBackendHost
{
    public sealed class EventBackendLayout
    {
        public readonly string Root, PythonPath, SourceRoot, EntryPath, OptionsPath, TempRoot;
        public EventBackendLayout()
        {
            Root = Path.Combine(ConfigStore.DirectoryPath, "event-backend");
            PythonPath = Path.Combine(Root, "python", "python.exe");
            SourceRoot = Path.Combine(Root, "src");
            EntryPath = Path.Combine(SourceRoot, "event_backend_entry.py");
            OptionsPath = Path.Combine(Root, "options.json");
            TempRoot = Path.Combine(Root, "temp");
        }
    }

    [DataContract]
    public sealed class BackendCredential
    {
        [DataMember(Name = "salt", IsRequired = true)] public readonly string Salt;
        [DataMember(Name = "hash", IsRequired = true)] public readonly string Hash;
        internal BackendCredential(string salt, string hash) { Salt = salt; Hash = hash; }
    }
    [DataContract]
    public sealed class BackendAppConfig
    {
        [DataMember(Name = "event_name", EmitDefaultValue = false)] public readonly string EventName;
        [DataMember(Name = "secret", IsRequired = true)] public readonly string Secret;
        [DataMember(Name = "admin_password", IsRequired = true)] public readonly BackendCredential AdminPassword;
        [DataMember(Name = "staff_password", IsRequired = true)] public readonly BackendCredential StaffPassword;
        internal BackendAppConfig(string name, string secret, BackendCredential admin, BackendCredential staff)
        { EventName = name; Secret = secret; AdminPassword = admin; StaffPassword = staff; }
    }
    public sealed class BackendOptions
    {
        public readonly string PublicOrigin, Schema;
        public readonly BackendAppConfig AppConfig;
        internal BackendOptions(string origin, BackendAppConfig config, string schema) { PublicOrigin = origin; AppConfig = config; Schema = schema; }
    }
    [DataContract]
    internal sealed class StartupPayload
    {
        [DataMember(IsRequired = true)] public string ConnectionString;
        [DataMember(IsRequired = true)] public string ApiToken;
        [DataMember(IsRequired = true)] public BackendAppConfig AppConfig;
        [DataMember(IsRequired = true)] public string PublicOrigin;
        [DataMember(IsRequired = true)] public string Schema;
    }

    public static class BackendConfiguration
    {
        public const int MaxOptionsBytes = 128 * 1024;
        private const string Driver18 = "ODBC Driver 18 for SQL Server", Driver17 = "ODBC Driver 17 for SQL Server";
        public static BackendOptions ParseOptions(byte[] bytes)
        {
            if (bytes == null || bytes.Length == 0 || bytes.Length > MaxOptionsBytes) throw new InvalidDataException("Invalid backend options.");
            new UTF8Encoding(false, true).GetString(bytes);
            XmlDictionaryReaderQuotas quotas = new XmlDictionaryReaderQuotas();
            quotas.MaxDepth = 8; quotas.MaxArrayLength = 32; quotas.MaxStringContentLength = 4096;
            XmlDocument document = new XmlDocument(); document.XmlResolver = null;
            using (XmlDictionaryReader reader = JsonReaderWriterFactory.CreateJsonReader(bytes, quotas)) document.Load(reader);
            Dictionary<string, XmlElement> root = Members(document.DocumentElement, new[] { "PublicOrigin", "AppConfig", "Schema" }, new[] { "PublicOrigin", "AppConfig" });
            string schema = root.ContainsKey("Schema") ? Text(root["Schema"]) : "eventdesk";
            if (schema != "eventdesk" && schema != "dbo") throw new InvalidDataException("Unsupported application schema.");
            string origin = Text(root["PublicOrigin"]);
            Uri uri;
            if (!Uri.TryCreate(origin, UriKind.Absolute, out uri) || uri.Scheme != "https" || uri.UserInfo.Length != 0
                || uri.AbsolutePath != "/" || uri.Query.Length != 0 || uri.Fragment.Length != 0)
                throw new InvalidDataException("Invalid backend origin.");
            Dictionary<string, XmlElement> config = Members(root["AppConfig"], new[] { "event_name", "secret", "admin_password", "staff_password" }, new[] { "secret", "admin_password", "staff_password" });
            string name = config.ContainsKey("event_name") ? Text(config["event_name"]) : null;
            if (name != null && (String.IsNullOrWhiteSpace(name) || name.Length > 200 || HasControl(name))) throw new InvalidDataException("Invalid event name.");
            return new BackendOptions(uri.GetLeftPart(UriPartial.Authority), new BackendAppConfig(name,
                Hex(Text(config["secret"]), 64), Credential(config["admin_password"]), Credential(config["staff_password"])), schema);
        }

        private static BackendCredential Credential(XmlElement element)
        {
            Dictionary<string, XmlElement> fields = Members(element, new[] { "salt", "hash" }, new[] { "salt", "hash" });
            return new BackendCredential(Hex(Text(fields["salt"]), 32), Hex(Text(fields["hash"]), 64));
        }
        private static string Hex(string value, int length)
        {
            if (value.Length != length || !Regex.IsMatch(value, "\\A[0-9A-Fa-f]+\\z", RegexOptions.CultureInvariant)) throw new InvalidDataException("Invalid application credential.");
            return value.ToLowerInvariant();
        }
        private static bool HasControl(string value) { foreach (char character in value) if (Char.IsControl(character)) return true; return false; }
        private static string Text(XmlElement element)
        {
            if (element.GetAttribute("type") != "string") throw new InvalidDataException("Invalid options field.");
            return element.InnerText;
        }
        private static Dictionary<string, XmlElement> Members(XmlElement element, string[] allowed, string[] required)
        {
            if (element == null || element.GetAttribute("type") != "object") throw new InvalidDataException("Invalid options object.");
            HashSet<string> names = new HashSet<string>(allowed, StringComparer.Ordinal);
            Dictionary<string, XmlElement> fields = new Dictionary<string, XmlElement>(StringComparer.Ordinal);
            foreach (XmlNode child in element.ChildNodes)
            {
                XmlElement value = child as XmlElement;
                if (value == null || !names.Contains(value.Name) || fields.ContainsKey(value.Name)) throw new InvalidDataException("Unexpected options field.");
                fields.Add(value.Name, value);
            }
            foreach (string name in required) if (!fields.ContainsKey(name)) throw new InvalidDataException("Missing options field.");
            return fields;
        }

        public static string SelectDriver(IEnumerable<string> names)
        {
            bool older = false;
            foreach (string name in names)
            {
                if (String.Equals(name, Driver18, StringComparison.OrdinalIgnoreCase)) return Driver18;
                if (String.Equals(name, Driver17, StringComparison.OrdinalIgnoreCase)) older = true;
            }
            if (older) return Driver17;
            throw new InvalidOperationException("A supported SQL Server ODBC driver is required.");
        }
        public static string InstalledDriver()
        {
            using (RegistryKey machine = RegistryKey.OpenBaseKey(RegistryHive.LocalMachine, RegistryView.Registry64))
            using (RegistryKey drivers = machine.OpenSubKey(@"SOFTWARE\ODBC\ODBCINST.INI\ODBC Drivers"))
            {
                List<string> installed = new List<string>();
                if (drivers != null) foreach (string name in drivers.GetValueNames())
                    if (String.Equals(drivers.GetValue(name) as string, "Installed", StringComparison.OrdinalIgnoreCase)) installed.Add(name);
                return SelectDriver(installed);
            }
        }
        public static string ToOdbc(string connectionString, string driver)
        {
            if (driver != Driver18 && driver != Driver17) throw new ArgumentException("Unsupported SQL driver.");
            SqlConnectionStringBuilder sql = new SqlConnectionStringBuilder(connectionString);
            if (!sql.Encrypt || !String.Equals(sql.InitialCatalog, "Employee_db", StringComparison.OrdinalIgnoreCase)
                || String.IsNullOrWhiteSpace(sql.DataSource) || sql.AttachDBFilename.Length != 0 || sql.UserInstance
                || (!sql.IntegratedSecurity && (String.IsNullOrEmpty(sql.UserID) || String.IsNullOrEmpty(sql.Password))))
                throw new InvalidDataException("Invalid SQL configuration.");
            OdbcConnectionStringBuilder odbc = new OdbcConnectionStringBuilder();
            odbc.Driver = driver; odbc["SERVER"] = sql.DataSource; odbc["DATABASE"] = sql.InitialCatalog;
            odbc["Encrypt"] = "Yes"; odbc["TrustServerCertificate"] = sql.TrustServerCertificate ? "Yes" : "No";
            odbc["MARS_Connection"] = "No"; odbc["Connection Timeout"] = 5; odbc["APP"] = "EventDeskEventBackend";
            if (sql.IntegratedSecurity) odbc["Trusted_Connection"] = "Yes";
            else { odbc["UID"] = sql.UserID; odbc["PWD"] = sql.Password; }
            string mapped = odbc.ConnectionString;
            if (mapped.Length > 8192) throw new InvalidDataException("SQL configuration is too large.");
            return mapped;
        }
    }

    public static class BackendHostSecurity
    {
        private const string SystemSid = "S-1-5-18", AdminSid = "S-1-5-32-544";
        public static void ValidateAcl(FileSystemSecurity security, bool protectedRoot)
        {
            if (security == null || (protectedRoot && !security.AreAccessRulesProtected)) throw new UnauthorizedAccessException("Backend permissions are not protected.");
            string owner = ((SecurityIdentifier)security.GetOwner(typeof(SecurityIdentifier))).Value;
            if (owner != SystemSid && owner != AdminSid) throw new UnauthorizedAccessException("Untrusted backend owner.");
            bool system = false, admin = false;
            foreach (FileSystemAccessRule rule in security.GetAccessRules(true, true, typeof(SecurityIdentifier)))
            {
                if (rule.AccessControlType != AccessControlType.Allow) continue;
                string sid = rule.IdentityReference.Value;
                if (sid != SystemSid && sid != AdminSid) throw new UnauthorizedAccessException("Untrusted backend permission.");
                if ((rule.FileSystemRights & FileSystemRights.FullControl) == FileSystemRights.FullControl)
                { if (sid == SystemSid) system = true; else admin = true; }
            }
            if (!system || !admin) throw new UnauthorizedAccessException("Privileged backend permissions are required.");
        }
        public static void ValidateAttributes(FileAttributes attributes, bool directory)
        {
            if ((attributes & FileAttributes.ReparsePoint) != 0 || ((attributes & FileAttributes.Directory) != 0) != directory)
                throw new UnauthorizedAccessException("Backend paths must be regular protected files and directories.");
        }
        public static void ValidateInstallation(EventBackendLayout layout)
        {
            for (DirectoryInfo ancestor = new DirectoryInfo(layout.Root); ancestor != null; ancestor = ancestor.Parent)
                ValidateAttributes(ancestor.Attributes, true);
            ValidateTree(layout.Root, true);
            ValidateAttributes(File.GetAttributes(layout.PythonPath), false);
            ValidateAttributes(File.GetAttributes(layout.EntryPath), false);
            ValidateAttributes(File.GetAttributes(layout.OptionsPath), false);
            ValidateAttributes(File.GetAttributes(layout.TempRoot), true);
        }
        private static void ValidateTree(string directory, bool root)
        {
            ValidateAttributes(File.GetAttributes(directory), true);
            ValidateAcl(Directory.GetAccessControl(directory, AccessControlSections.Owner | AccessControlSections.Access), root);
            foreach (string entry in Directory.EnumerateFileSystemEntries(directory))
            {
                FileAttributes attributes = File.GetAttributes(entry);
                if ((attributes & FileAttributes.Directory) != 0) ValidateTree(entry, false);
                else
                {
                    ValidateAttributes(attributes, false);
                    ValidateAcl(File.GetAccessControl(entry, AccessControlSections.Owner | AccessControlSections.Access), false);
                }
            }
        }
        public static byte[] ReadOptions(string path)
        {
            using (FileStream file = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.Read))
            {
                if (file.Length <= 0 || file.Length > BackendConfiguration.MaxOptionsBytes) throw new InvalidDataException("Invalid options size.");
                byte[] bytes = new byte[(int)file.Length]; int offset = 0;
                while (offset < bytes.Length) { int read = file.Read(bytes, offset, bytes.Length - offset); if (read == 0) throw new InvalidDataException("Incomplete options."); offset += read; }
                return bytes;
            }
        }
    }

    public sealed class BackendProcessPlan : IDisposable
    {
        public readonly ProcessStartInfo StartInfo;
        public readonly byte[] Payload;
        private BackendProcessPlan(ProcessStartInfo info, byte[] payload) { StartInfo = info; Payload = payload; }
        public static BackendProcessPlan Create(EventBackendLayout layout, BackendOptions options, SecretSettings secret, string driver)
        {
            SecretSettings validated = ConfigStore.Validate(secret);
            StartupPayload value = new StartupPayload { ConnectionString = BackendConfiguration.ToOdbc(validated.ConnectionString, driver), ApiToken = validated.ApiToken, AppConfig = options.AppConfig, PublicOrigin = options.PublicOrigin, Schema = options.Schema };
            byte[] payload;
            using (MemoryStream memory = new MemoryStream()) { new DataContractJsonSerializer(typeof(StartupPayload)).WriteObject(memory, value); payload = memory.ToArray(); }
            if (payload.Length > BackendConfiguration.MaxOptionsBytes) { Array.Clear(payload, 0, payload.Length); throw new InvalidDataException("Startup configuration is too large."); }
            ProcessStartInfo info = new ProcessStartInfo(layout.PythonPath, "-I \"" + layout.EntryPath + "\" --config-stdin");
            info.WorkingDirectory = layout.SourceRoot; info.UseShellExecute = false; info.CreateNoWindow = true;
            info.RedirectStandardInput = true; info.RedirectStandardOutput = true; info.RedirectStandardError = true;
            info.StandardOutputEncoding = new UTF8Encoding(false); info.StandardErrorEncoding = new UTF8Encoding(false);
            string windows = Environment.GetFolderPath(Environment.SpecialFolder.Windows);
            info.EnvironmentVariables.Clear(); info.EnvironmentVariables["SystemRoot"] = windows; info.EnvironmentVariables["WINDIR"] = windows;
            info.EnvironmentVariables["PATH"] = Path.GetDirectoryName(layout.PythonPath) + ";" + Path.Combine(windows, "System32") + ";" + windows;
            info.EnvironmentVariables["TEMP"] = layout.TempRoot; info.EnvironmentVariables["TMP"] = layout.TempRoot;
            return new BackendProcessPlan(info, payload);
        }
        public void Dispose() { Array.Clear(Payload, 0, Payload.Length); }
        public static void WriteInput(Stream input, byte[] payload)
        { WriteInput(input, payload, 15000, null); }
        public static void WriteInput(Stream input, byte[] payload, int timeoutMilliseconds, Action abort)
        {
            if (input == null || payload == null || payload.Length == 0 || payload.Length > BackendConfiguration.MaxOptionsBytes)
                throw new InvalidDataException("Invalid startup payload.");
            if (timeoutMilliseconds < 1 || timeoutMilliseconds > 15000) throw new ArgumentException("Invalid startup deadline.");
            // Write UTF8 bytes directly: .NET Framework has no StandardInputEncoding
            // setting, and its default StreamWriter may use a legacy OEM code page.
            using (input)
            {
                Task writing = Task.Factory.StartNew(delegate { input.Write(payload, 0, payload.Length); input.Flush(); });
                if (!writing.Wait(timeoutMilliseconds))
                {
                    // Closing the job first terminates readers/descendants holding the
                    // pipe. This releases blocked writes before disposing the stream.
                    if (abort != null) abort();
                    writing.ContinueWith(delegate(Task completed) { GC.KeepAlive(completed.Exception); }, TaskContinuationOptions.OnlyOnFaulted);
                    throw new System.TimeoutException("Backend startup input timed out.");
                }
            }
        }
    }

    internal sealed class ChildJob : IDisposable
    {
        private readonly JobHandle handle;
        public ChildJob()
        {
            handle = CreateJobObject(IntPtr.Zero, null);
            if (handle.IsInvalid) { handle.Dispose(); throw new InvalidOperationException("Backend containment failed."); }
            ExtendedLimits limits = new ExtendedLimits(); limits.Basic.LimitFlags = 0x2000; // JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if (!SetInformationJobObject(handle, 9, ref limits, (uint)Marshal.SizeOf(typeof(ExtendedLimits))))
            { handle.Dispose(); throw new InvalidOperationException("Backend containment failed."); }
        }
        public void Assign(Process process)
        {
            if (!AssignProcessToJobObject(handle, process.Handle)) throw new InvalidOperationException("Backend containment failed.");
        }
        public void Dispose() { handle.Dispose(); }
        private sealed class JobHandle : SafeHandleZeroOrMinusOneIsInvalid
        {
            public JobHandle() : base(true) { }
            protected override bool ReleaseHandle() { return CloseHandle(handle); }
        }
        [StructLayout(LayoutKind.Sequential)] private struct BasicLimits
        { public long ProcessTime, JobTime; public uint LimitFlags; public UIntPtr MinWorkingSet, MaxWorkingSet; public uint ActiveProcessLimit; public UIntPtr Affinity; public uint PriorityClass, SchedulingClass; }
        [StructLayout(LayoutKind.Sequential)] private struct IoCounters
        { public ulong ReadOps, WriteOps, OtherOps, ReadBytes, WriteBytes, OtherBytes; }
        [StructLayout(LayoutKind.Sequential)] private struct ExtendedLimits
        { public BasicLimits Basic; public IoCounters Io; public UIntPtr ProcessMemory, JobMemory, PeakProcessMemory, PeakJobMemory; }
        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)] private static extern JobHandle CreateJobObject(IntPtr attributes, string name);
        [DllImport("kernel32.dll", SetLastError = true)] private static extern bool SetInformationJobObject(JobHandle job, int infoClass, ref ExtendedLimits information, uint length);
        [DllImport("kernel32.dll", SetLastError = true)] private static extern bool AssignProcessToJobObject(JobHandle job, IntPtr process);
        [DllImport("kernel32.dll", SetLastError = true)] private static extern bool CloseHandle(IntPtr handle);
    }

    public sealed class BackendSupervisor : IDisposable
    {
        public const int StopWaitMilliseconds = 35000, ServiceGraceMilliseconds = 45000;
        private readonly object lifecycle = new object();
        private readonly ISafeLog log;
        private readonly Action unexpectedExit;
        private Process child;
        private ChildJob job;
        private string apiToken;
        private volatile bool stopping;
        public BackendSupervisor(ISafeLog safeLog, Action onUnexpectedExit) { if (safeLog == null) throw new ArgumentNullException("safeLog"); log = safeLog; unexpectedExit = onUnexpectedExit; }
        public void Start()
        {
            lock (lifecycle)
            {
                if (child != null || stopping) throw new InvalidOperationException("Backend cannot restart in place.");
                try
                {
                    EventBackendLayout layout = new EventBackendLayout(); BackendHostSecurity.ValidateInstallation(layout);
                    byte[] raw = BackendHostSecurity.ReadOptions(layout.OptionsPath); BackendOptions options;
                    try { options = BackendConfiguration.ParseOptions(raw); } finally { Array.Clear(raw, 0, raw.Length); }
                    SecretSettings secret = ConfigStore.Load(); apiToken = secret.ApiToken;
                    using (BackendProcessPlan plan = BackendProcessPlan.Create(layout, options, secret, BackendConfiguration.InstalledDriver()))
                    {
                        job = new ChildJob(); child = new Process(); child.StartInfo = plan.StartInfo; child.EnableRaisingEvents = true;
                        child.Exited += OnExited;
                        if (!child.Start()) throw new InvalidOperationException("Backend process failed to start.");
                        job.Assign(child); // The Python entry receives no configuration until containment succeeds.
                        Drain(child.StandardOutput, false); Drain(child.StandardError, true);
                        BackendProcessPlan.WriteInput(child.StandardInput.BaseStream, plan.Payload, 15000,
                            delegate { stopping = true; KillAndDispose(); });
                        if (child.HasExited) throw new InvalidOperationException("Backend process exited during startup.");
                    }
                    Log("event_backend_started");
                }
                catch
                {
                    stopping = true; KillAndDispose(); Log("event_backend_start_failed");
                    throw new InvalidOperationException("Event backend startup failed. Check protected installation.");
                }
            }
        }
        private void OnExited(object sender, EventArgs args)
        {
            if (stopping) return;
            Log("event_backend_exited");
            if (unexpectedExit != null) ThreadPool.QueueUserWorkItem(delegate { if (!stopping) unexpectedExit(); });
        }
        private void Drain(StreamReader stream, bool report)
        {
            Thread draining = new Thread(delegate()
            {
                char[] buffer = new char[1024]; bool reported = false;
                try { while (stream.Read(buffer, 0, buffer.Length) > 0) { if (report && !reported) { Log("event_backend_stderr"); reported = true; } Array.Clear(buffer, 0, buffer.Length); } }
                catch (IOException) { if (!stopping) Log("event_backend_stream_error"); }
                catch (ObjectDisposedException) { }
                finally { Array.Clear(buffer, 0, buffer.Length); }
            });
            draining.IsBackground = true; draining.Name = "EventBackendOutput"; draining.Start();
        }
        private void Log(string category) { try { log.Write(category, 0); } catch (Exception) { Trace.WriteLine("Event backend log unavailable."); } }
        private bool Running() { try { return child != null && !child.HasExited; } catch (InvalidOperationException) { return false; } }
        private void SignalShutdown()
        {
            SendShutdown(CreateShutdownRequest());
        }
        private HttpWebRequest CreateShutdownRequest()
        {
            return BuildShutdownRequest(new Uri("http://127.0.0.1:8766/__service/shutdown"));
        }
        private HttpWebRequest BuildShutdownRequest(Uri endpoint)
        {
            HttpWebRequest request = (HttpWebRequest)WebRequest.Create(endpoint);
            request.Method = "POST"; request.Proxy = null; request.AllowAutoRedirect = false; request.UseDefaultCredentials = false;
            request.Timeout = 2000; request.ReadWriteTimeout = 2000; request.KeepAlive = false;
            request.Headers["Authorization"] = "Bearer " + apiToken; request.ContentType = "application/json";
            request.ContentLength = 0; request.SendChunked = false;
            return request;
        }
        private void SendShutdown(HttpWebRequest request)
        {
            try
            {
                using (WebResponse response = request.GetResponse()) { }
            }
            catch (Exception) { Log("event_shutdown_signal_failed"); }
            finally { request.Abort(); }
        }
        private void KillAndDispose()
        {
            if (job != null) { job.Dispose(); job = null; }
            if (child != null)
            {
                try { if (Running()) child.Kill(); if (!child.HasExited) child.WaitForExit(5000); }
                catch (InvalidOperationException) { }
                catch (System.ComponentModel.Win32Exception) { Log("event_backend_kill_failed"); }
                finally { child.Dispose(); child = null; }
            }
            apiToken = null;
        }
        public void Dispose()
        {
            lock (lifecycle)
            {
                if (stopping) return;
                stopping = true;
                try { if (Running()) { SignalShutdown(); child.WaitForExit(StopWaitMilliseconds); } }
                finally { KillAndDispose(); }
            }
        }
    }

    internal sealed class EventBackendWindowsService : ServiceBase
    {
        private BackendSupervisor backend;
        public EventBackendWindowsService() { ServiceName = "EventDeskEventBackend"; AutoLog = false; CanStop = true; CanShutdown = true; }
        protected override void OnStart(string[] args)
        {
            RequestAdditionalTime(60000);
            backend = new BackendSupervisor(new RollingSafeLog(Path.Combine(ConfigStore.DirectoryPath, "logs")), delegate { ExitCode = 1; try { Stop(); } catch (InvalidOperationException) { } });
            backend.Start();
        }
        protected override void OnStop() { if (backend != null) { RequestAdditionalTime(BackendSupervisor.ServiceGraceMilliseconds); backend.Dispose(); backend = null; } }
        protected override void OnShutdown() { if (backend != null) { backend.Dispose(); backend = null; } base.OnShutdown(); }
        protected override void Dispose(bool disposing) { if (disposing && backend != null) { backend.Dispose(); backend = null; } base.Dispose(disposing); }
    }
    internal static class EventBackendProgram
    {
        private static int Main(string[] args)
        {
            try
            {
                if (args.Length == 0) { ServiceBase.Run(new EventBackendWindowsService()); return 0; }
                if (args.Length == 1 && args[0] == "--console")
                {
                    using (ManualResetEvent stop = new ManualResetEvent(false))
                    using (BackendSupervisor backend = new BackendSupervisor(new RollingSafeLog(Path.Combine(ConfigStore.DirectoryPath, "logs")), delegate { stop.Set(); }))
                    {
                        ConsoleCancelEventHandler cancel = delegate(object sender, ConsoleCancelEventArgs eventArgs) { eventArgs.Cancel = true; stop.Set(); };
                        Console.CancelKeyPress += cancel;
                        try { backend.Start(); Console.WriteLine("Private event backend started. Press Ctrl+C to stop."); stop.WaitOne(); }
                        finally { Console.CancelKeyPress -= cancel; }
                    }
                    return 0;
                }
                Console.Error.WriteLine("Usage: EventBackendService.exe [--console]."); return 2;
            }
            catch (Exception) { Console.Error.WriteLine("Event backend operation failed. Check protected installation."); return 1; }
        }
    }
}
