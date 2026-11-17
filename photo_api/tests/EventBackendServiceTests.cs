using System;
using System.Collections.Generic;
using System.Data.Odbc;
using System.Diagnostics;
using System.IO;
using System.Net;
using System.Net.Sockets;
using System.Reflection;
using System.Security.AccessControl;
using System.Security.Principal;
using System.Text;
using System.Threading;
using EventDesk.PhotoApi;
using EventDesk.EventBackendHost;

internal static class EventBackendServiceTests
{
    private static int assertions;
    private static int Main()
    {
        try { TestOptions(); TestOdbc(); TestPermissions(); TestProcessPlan(); TestInputDeadline(); TestShutdownSignalContract(); Console.WriteLine("PASS: " + assertions + " backend service checks; no SQL, DPAPI, service or private configuration access."); return 0; }
        catch (Exception error) { Console.Error.WriteLine("FAIL: " + error.Message); return 1; }
    }
    private static string Config(string origin)
    {
        return "{\"PublicOrigin\":\"" + origin + "\",\"AppConfig\":{\"event_name\":\"Test Event\",\"secret\":\"" + new string('a',64)
            + "\",\"admin_password\":{\"salt\":\"" + new string('b',32) + "\",\"hash\":\"" + new string('c',64)
            + "\"},\"staff_password\":{\"salt\":\"" + new string('d',32) + "\",\"hash\":\"" + new string('e',64) + "\"}}}";
    }
    private static BackendOptions Options() { return BackendConfiguration.ParseOptions(Encoding.UTF8.GetBytes(Config("https://eventdesk.pages.dev"))); }
    private static void TestOptions()
    {
        BackendOptions options = Options();
        Check(options.PublicOrigin == "https://eventdesk.pages.dev", "Valid HTTPS origin retained");
        Check(options.Schema == "eventdesk", "Dedicated schema is the default");
        Check(BackendConfiguration.ParseOptions(Encoding.UTF8.GetBytes(Config("https://example.invalid").Replace("\"PublicOrigin\":", "\"Schema\":\"dbo\",\"PublicOrigin\":"))).Schema == "dbo", "Existing dbo schema explicitly supported");
        Reject(delegate { BackendConfiguration.ParseOptions(Encoding.UTF8.GetBytes(Config("https://example.invalid").Replace("\"PublicOrigin\":", "\"Schema\":\"dbo;drop table\",\"PublicOrigin\":"))); }, "Caller cannot inject arbitrary schema identifiers");
        Check(options.AppConfig.Secret.Length == 64 && options.AppConfig.AdminPassword.Hash.Length == 64, "Real SHA256 application credential shape retained");
        foreach (string origin in new[] { "http://eventdesk.pages.dev", "https://example.invalid/path", "https://u:p@example.invalid", "https://example.invalid/?x=1", "https://example.invalid/#x", "file:///C:/test" })
            Reject(delegate { BackendConfiguration.ParseOptions(Encoding.UTF8.GetBytes(Config(origin))); }, "Non-origin URL rejected");
        foreach (string value in new[] { Config("https://example.invalid").Replace(new string('c',64), new string('c',128)),
            Config("https://example.invalid").Replace(new string('b',32), "invalid"),
            Config("https://example.invalid").Replace("\"PublicOrigin\":", "\"CodePath\":\"evil\",\"PublicOrigin\":"),
            Config("https://example.invalid").Replace("\"PublicOrigin\":", "\"PublicOrigin\":\"https://duplicate.invalid\",\"PublicOrigin\":"),
            "{}", "null", "broken-json" })
            Reject(delegate { BackendConfiguration.ParseOptions(Encoding.UTF8.GetBytes(value)); }, "Malformed/unknown/duplicate options rejected");
        Reject(delegate { BackendConfiguration.ParseOptions(new byte[128 * 1024 + 1]); }, "Options bounded before parsing");
        Reject(delegate { BackendConfiguration.ParseOptions(new byte[] {255,255}); }, "Malformed UTF8 rejected");
    }
    private static void TestOdbc()
    {
        Check(BackendConfiguration.SelectDriver(new[] { "ODBC Driver 17 for SQL Server", "ODBC Driver 18 for SQL Server" }) == "ODBC Driver 18 for SQL Server", "Driver18 preferred");
        Check(BackendConfiguration.SelectDriver(new[] { "ODBC Driver 17 for SQL Server" }) == "ODBC Driver 17 for SQL Server", "Driver17 fallback supported");
        Reject(delegate { BackendConfiguration.SelectDriver(new string[0]); }, "Missing driver fails closed");
        System.Data.SqlClient.SqlConnectionStringBuilder source = new System.Data.SqlClient.SqlConnectionStringBuilder();
        source.DataSource = "synthetic.invalid"; source.InitialCatalog = "Employee_db"; source.Encrypt = true;
        source.TrustServerCertificate = true; source.UserID = "synthetic-user"; source.Password = "p;a{ss}word=\"test";
        string mapped = BackendConfiguration.ToOdbc(source.ConnectionString, "ODBC Driver 18 for SQL Server");
        OdbcConnectionStringBuilder value = new OdbcConnectionStringBuilder(mapped);
        string encodedPassword = (string)value["PWD"];
        // OdbcConnectionStringBuilder intentionally retains ODBC brace syntax in
        // indexer values; SQLDriverConnect, not the managed builder, decodes it.
        string decodedPassword = encodedPassword.StartsWith("{") && encodedPassword.EndsWith("}")
            ? encodedPassword.Substring(1, encodedPassword.Length - 2).Replace("}}", "}") : encodedPassword;
        Check(decodedPassword == source.Password && (string)value["UID"] == source.UserID, "Typed ODBC builder safely escapes semicolons, braces and quotes");
        Check(mapped.Contains("PWD={p;a{ss}}word=\"test}"), "ODBC password uses escaped closing braces rather than adding attributes");
        Check((string)value["DATABASE"] == "Employee_db" && (string)value["SERVER"] == "synthetic.invalid", "Database/server mapped explicitly");
        Check((string)value["Encrypt"] == "Yes" && (string)value["TrustServerCertificate"] == "Yes", "TLS configuration preserved");
        Check((string)value["MARS_Connection"] == "No" && Convert.ToString(value["Connection Timeout"]) == "5", "No MARS and bounded connection timeout");
        source.IntegratedSecurity = true;
        OdbcConnectionStringBuilder integrated = new OdbcConnectionStringBuilder(BackendConfiguration.ToOdbc(source.ConnectionString, "ODBC Driver 17 for SQL Server"));
        Check((string)integrated["Trusted_Connection"] == "Yes" && !integrated.ContainsKey("UID") && !integrated.ContainsKey("PWD"), "Integrated SQL auth does not copy credentials");
        source.Encrypt = false;
        Reject(delegate { BackendConfiguration.ToOdbc(source.ConnectionString, "ODBC Driver 18 for SQL Server"); }, "Unencrypted source rejected");
        source.Encrypt = true; source.InitialCatalog = "master";
        Reject(delegate { BackendConfiguration.ToOdbc(source.ConnectionString, "ODBC Driver 18 for SQL Server"); }, "Wrong database rejected");
        Reject(delegate { BackendConfiguration.ToOdbc(source.ConnectionString, "attacker-driver"); }, "Unapproved driver rejected");
    }
    private static void TestPermissions()
    {
        DirectorySecurity directory = new DirectorySecurity(); directory.SetAccessRuleProtection(true, false);
        directory.SetOwner(new SecurityIdentifier("S-1-5-32-544"));
        foreach (string sid in new[] { "S-1-5-18", "S-1-5-32-544" }) directory.AddAccessRule(new FileSystemAccessRule(new SecurityIdentifier(sid), FileSystemRights.FullControl, InheritanceFlags.ContainerInherit | InheritanceFlags.ObjectInherit, PropagationFlags.None, AccessControlType.Allow));
        BackendHostSecurity.ValidateAcl(directory, true); Check(true, "Protected privileged-only root accepted");
        FileSecurity file = new FileSecurity(); file.SetAccessRuleProtection(true, false); file.SetOwner(new SecurityIdentifier("S-1-5-18"));
        foreach (string sid in new[] { "S-1-5-18", "S-1-5-32-544" }) file.AddAccessRule(new FileSystemAccessRule(new SecurityIdentifier(sid), FileSystemRights.FullControl, AccessControlType.Allow));
        BackendHostSecurity.ValidateAcl(file, false); Check(true, "Privileged code file accepted");
        foreach (string sid in new[] { "S-1-1-0", "S-1-5-11", "S-1-5-32-545" })
        {
            FileSecurity unsafeFile = new FileSecurity(); unsafeFile.SetSecurityDescriptorBinaryForm(file.GetSecurityDescriptorBinaryForm());
            unsafeFile.AddAccessRule(new FileSystemAccessRule(new SecurityIdentifier(sid), FileSystemRights.Read, AccessControlType.Allow));
            Reject(delegate { BackendHostSecurity.ValidateAcl(unsafeFile, false); }, "Even untrusted read permission is rejected");
        }
        directory.SetAccessRuleProtection(false, false);
        Reject(delegate { BackendHostSecurity.ValidateAcl(directory, true); }, "Inherited root DACL rejected");
        file.SetOwner(new SecurityIdentifier("S-1-5-21-100-200-300-1001"));
        Reject(delegate { BackendHostSecurity.ValidateAcl(file, false); }, "Untrusted owner cannot replace code ACL");
        Reject(delegate { BackendHostSecurity.ValidateAttributes(FileAttributes.Directory | FileAttributes.ReparsePoint, true); }, "Junction/symlink rejected");
        Reject(delegate { BackendHostSecurity.ValidateAttributes(FileAttributes.Directory, false); }, "Code/options must be files");
    }
    private static void TestProcessPlan()
    {
        EventBackendLayout layout = new EventBackendLayout();
        string apiToken = Guid.NewGuid().ToString("N") + "-fixture-alphabet";
        SecretSettings settings = new SecretSettings { ConnectionString = "Server=synthetic.invalid;Initial Catalog=Employee_db;User ID=test;Password=synthetic-only;Encrypt=True;TrustServerCertificate=True", ApiToken = apiToken, Port = 8765 };
        using (BackendProcessPlan plan = BackendProcessPlan.Create(layout, Options(), settings, "ODBC Driver 18 for SQL Server"))
        {
            ProcessStartInfo start = plan.StartInfo;
            Check(start.FileName == Path.Combine(layout.Root, "python", "python.exe"), "Interpreter path is fixed beneath protected root");
            Check(start.WorkingDirectory == layout.SourceRoot && start.Arguments == "-I \"" + layout.EntryPath + "\" --config-stdin", "Entry point is fixed and isolated mode is mandatory");
            Check(!start.UseShellExecute && start.CreateNoWindow && start.RedirectStandardInput && start.RedirectStandardOutput && start.RedirectStandardError, "Child uses private stdin and no shell/window");
            Check(!start.Arguments.Contains(apiToken) && !start.Arguments.Contains("synthetic-only"), "Credentials never enter command arguments");
            Check(!start.EnvironmentVariables.ContainsKey("PYTHONPATH") && !start.EnvironmentVariables.ContainsKey("EVENTDESK_IMPORT_CONNECTION"), "Inherited runtime/credential environment stripped");
            string json = Encoding.UTF8.GetString(plan.Payload);
            Check(json.Contains("\"ConnectionString\"") && json.Contains("\"ApiToken\"") && json.Contains("\"AppConfig\"") && json.Contains("\"PublicOrigin\""), "Only expected startup configuration is serialized");
            Check(plan.Payload.Length < 128 * 1024, "Startup payload remains bounded");
            MemoryStream pipe = new MemoryStream(); BackendProcessPlan.WriteInput(pipe, plan.Payload);
            Check(Convert.ToBase64String(pipe.ToArray()) == Convert.ToBase64String(plan.Payload), "Stdin preserves exact UTF8 bytes and closes for EOF");
            Check(BackendSupervisor.StopWaitMilliseconds == 35000 && BackendSupervisor.ServiceGraceMilliseconds == 45000, "Grace period accommodates in-flight SQL work");
        }
    }
    private static void Reject(Action action, string message) { bool rejected = false; try { action(); } catch (Exception) { rejected = true; } Check(rejected, message); }
    private static void TestInputDeadline()
    {
        BlockingInput stream = new BlockingInput(); bool aborted = false, timedOut = false;
        Stopwatch elapsed = Stopwatch.StartNew();
        try { BackendProcessPlan.WriteInput(stream, Encoding.UTF8.GetBytes("synthetic"), 25, delegate { aborted = true; stream.Release.Set(); }); }
        catch (TimeoutException) { timedOut = true; }
        Check(timedOut && aborted && stream.Closed, "Blocked startup write aborts child before closing stdin");
        Check(elapsed.ElapsedMilliseconds < 2000, "Startup deadline bounds a blocked pipe");
    }
    private sealed class BlockingInput : MemoryStream
    {
        public readonly ManualResetEvent Release = new ManualResetEvent(false);
        public bool Closed;
        public override void Write(byte[] buffer, int offset, int count) { Release.WaitOne(5000); }
        protected override void Dispose(bool disposing) { Closed = true; Release.Set(); base.Dispose(disposing); }
    }
    private static void TestShutdownSignalContract()
    {
        RecordingLog log = new RecordingLog();
        using (BackendSupervisor supervisor = new BackendSupervisor(log, null))
        {
            const BindingFlags flags = BindingFlags.Instance | BindingFlags.NonPublic;
            typeof(BackendSupervisor).GetField("apiToken", flags).SetValue(supervisor, "synthetic-shutdown-token");
            MethodInfo factory = typeof(BackendSupervisor).GetMethod("CreateShutdownRequest", flags);
            Check(factory != null, "Production shutdown request factory exists");
            HttpWebRequest production = (HttpWebRequest)factory.Invoke(supervisor, null);
            Check(production.RequestUri.AbsoluteUri == "http://127.0.0.1:8766/__service/shutdown", "Shutdown endpoint remains fixed and private");
            Check(production.Method == "POST" && production.ContentLength == 0 && !production.SendChunked, "Shutdown uses POST with explicit zero length and no chunking");
            Check(production.Headers["Authorization"] == "Bearer synthetic-shutdown-token" && !production.AllowAutoRedirect && production.Proxy == null, "Shutdown authenticates without redirect or proxy");
            production.Abort();
            TcpListener reservation = new TcpListener(IPAddress.Loopback, 0); reservation.Start();
            int port = ((IPEndPoint)reservation.LocalEndpoint).Port; reservation.Stop();
            using (HttpListener listener = new HttpListener())
            {
                string origin = "http://127.0.0.1:" + port + "/"; listener.Prefixes.Add(origin); listener.Start();
                HttpWebRequest request = (HttpWebRequest)typeof(BackendSupervisor).GetMethod("BuildShutdownRequest", flags).Invoke(supervisor, new object[] { new Uri(origin + "__service/shutdown") });
                Exception sendError = null;
                Thread sender = new Thread(delegate()
                {
                    try { typeof(BackendSupervisor).GetMethod("SendShutdown", flags).Invoke(supervisor, new object[] { request }); }
                    catch (Exception error) { sendError = error; }
                });
                sender.IsBackground = true; sender.Start();
                IAsyncResult pending = listener.BeginGetContext(null, null);
                Check(pending.AsyncWaitHandle.WaitOne(5000), "Shutdown request reaches owned loopback listener");
                HttpListenerContext context = listener.EndGetContext(pending);
                Check(context.Request.HttpMethod == "POST" && context.Request.RawUrl == "/__service/shutdown", "Wire method and path match Python control route");
                Check(context.Request.Headers["Content-Length"] == "0" && context.Request.ContentLength64 == 0 && context.Request.Headers["Transfer-Encoding"] == null, "Wire framing matches strict backend zero-length/no-TE contract");
                Check(context.Request.InputStream.ReadByte() == -1 && context.Request.Headers["Authorization"] == "Bearer synthetic-shutdown-token", "Wire body is empty and bearer is retained");
                context.Response.StatusCode = 200; context.Response.ContentLength64 = 0; context.Response.Close();
                Check(sender.Join(5000) && sendError == null && log.Last == null, "Production sender completes cleanly without shutdown failure log");
            }
        }
    }
    private sealed class RecordingLog : ISafeLog { public string Last; public void Write(string category, int number) { Last = category; } }
    private static void Check(bool value, string message) { if (!value) throw new InvalidOperationException(message); assertions++; }
}
